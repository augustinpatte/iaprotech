"""
File processor — extraction, chunking et pseudonymisation multi-formats.

Architecture :
  ① extract_text(file_bytes, filename) → str
       Détecte l'extension, extrait le texte entièrement en mémoire (jamais de
       fichier temporaire), retourne le texte brut.
       • PDF  : PyMuPDF page par page, ordre de lecture préservé
       • DOCX : paragraphes + tableaux dans l'ordre du document ; cellules
                jointes par " | " ; cellules fusionnées dédupliquées
       • TXT  : décodage UTF-8 (fallback Latin-1)

  ② chunk_text(text, chunk_size, overlap) → List[str]
       Découpe le texte en chunks qui se chevauchent.
       Priorité des points de coupe : \\n\\n > fin de phrase > \\n > espace > hard-split.
       L'overlap de 200 chars garantit qu'aucune entité nommée n'est coupée.

  ③ process_file(file_bytes, filename, redactor, vault, session_id) → dict
       Pipeline complet :
         extract_text → chunk_text → smart_pseudonymize × N → vault.store_mapping
       Retourne les métadonnées sans jamais exposer le contenu dans les logs.

Sécurité RGPD :
  • Traitement 100 % in-memory (BytesIO, pas de tempfile)
  • Limite 10 Mo vérifiée avant extraction
  • Logs zero-content : taille, extension, compteurs uniquement

Compatibilité ascendante :
  • La classe FileProcessor est maintenue pour les routes legacy.
"""

from __future__ import annotations

import asyncio
import io
import os
import re as _re
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, TypedDict

import fitz                        # PyMuPDF
import docx as python_docx         # python-docx top-level package
from docx import Document as DocxDocument
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph as DocxParagraph
from docx.table import Table as DocxTable
from fastapi import HTTPException, status

from app.config import settings
from app.utils.logger import get_logger, hash_id as _h

if TYPE_CHECKING:
    from app.core.redactor import Redactor
    from app.core.vault import Vault

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

_MAX_BYTES: int = 20 * 1024 * 1024          # 20 Mo — limite absolue du module
_CHARS_PER_TOKEN: int = 4                   # estimation grossière, indépendante de la langue
CPU_COUNT = os.cpu_count() or 2
_MAX_EXTRACTED_CHARS: int = 50_000

# Executor dédié à l'extraction (fitz + python-docx sont synchrones et CPU-bound)
_executor = ThreadPoolExecutor(
    max_workers=min(CPU_COUNT * 2, 8),
    thread_name_prefix="file_proc",
)
MAX_UNCOMPRESSED_SIZE: int = 200 * 1024 * 1024
MAX_ZIP_ENTRIES: int = 1000

# Tags XML python-docx
_TAG_P   = qn("w:p")    # paragraphe
_TAG_TBL = qn("w:tbl")  # tableau
_TAG_TR  = qn("w:tr")   # ligne de tableau
_TAG_TC  = qn("w:tc")   # cellule de tableau

_DOCX_NS: dict[str, str] = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
}


# ---------------------------------------------------------------------------
# ① Extraction synchrone (exécutée dans le thread pool)
# ---------------------------------------------------------------------------

def check_zip_safety(file_bytes: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            entries = zf.infolist()
            if len(entries) > MAX_ZIP_ENTRIES:
                raise ValueError(f"Archive suspecte : {len(entries)} entrees")

            total_uncompressed = sum(entry.file_size for entry in entries)
            if total_uncompressed > MAX_UNCOMPRESSED_SIZE:
                raise ValueError(
                    "Archive suspecte : "
                    f"{total_uncompressed // 1024 // 1024}Mo decompresse"
                )

            max_ratio = max(
                (entry.file_size / max(entry.compress_size, 1)) for entry in entries
            ) if entries else 0
            if max_ratio > 100:
                raise ValueError(f"Ratio de compression suspect : {max_ratio:.0f}x")
    except zipfile.BadZipFile as exc:
        raise ValueError("Fichier ZIP invalide ou corrompu") from exc


def _warn_if_zip_contains_macros(file_bytes: bytes, label: str) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            names = {name.lower() for name in zf.namelist()}
            if any("vbaproject.bin" in name or name.endswith("/macros.xml") for name in names):
                logger.warning("archive.macros_detected | type=%s | action=text_only", label.lower())
    except zipfile.BadZipFile:
        return


def _extract_pdf_sync(content: bytes) -> str:
    """
    Extrait le texte d'un PDF via PyMuPDF, page par page.
    Conserve l'ordre de lecture natif de fitz ("text" layout).
    Lève ValueError pour les PDFs corrompus, vides ou chiffrés.
    """
    try:
        doc = fitz.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise ValueError(
            f"PDF invalide ou corrompu : {type(exc).__name__}"
        ) from exc

    if doc.is_encrypted:
        doc.close()
        raise ValueError("PDF chiffré : mot de passe requis — non supporté.")

    pages: list[str] = []
    for page_num, page in enumerate(doc, start=1):
        try:
            page_text = page.get_text("text")  # ordre de lecture naturel
        except Exception:
            continue  # page illisible → ignorée (PDF hybride image + texte)
        if page_text.strip():
            pages.append(f"[Page {page_num}]\n{page_text.strip()}")
    doc.close()

    if not pages:
        raise ValueError(
            "Le PDF ne contient aucun texte extractible "
            "(PDF scanné ou composé uniquement d'images)."
        )
    return "\n\n".join(pages)


def _iter_docx_blocks(document: DocxDocument):
    """
    Itère sur les éléments de niveau racine du body DOCX dans l'ordre du document.
    Yield : ('p', DocxParagraph) ou ('tbl', DocxTable)
    Permet d'intercaler paragraphes et tableaux dans leur ordre réel.
    """
    for child in document.element.body.iterchildren():
        tag = child.tag
        if tag == _TAG_P:
            yield "p", DocxParagraph(child, document)
        elif tag == _TAG_TBL:
            yield "tbl", DocxTable(child, document)


def _extract_docx_xml_text(xml_bytes: bytes, paths: list[str]) -> list[str]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    parts: list[str] = []
    for path in paths:
        for node in root.findall(path, _DOCX_NS):
            texts = [text.strip() for text in node.itertext() if text and text.strip()]
            if texts:
                parts.append(" ".join(texts))
    return parts


def _extract_docx_structured_sections(content: bytes) -> list[str]:
    sections: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            header_parts: list[str] = []
            footer_parts: list[str] = []
            comment_parts: list[str] = []
            revision_parts: list[str] = []
            text_box_parts: list[str] = []

            for name in archive.namelist():
                if not name.startswith("word/") or not name.endswith(".xml"):
                    continue

                xml_bytes = archive.read(name)
                if name.startswith("word/header"):
                    header_parts.extend(_extract_docx_xml_text(xml_bytes, [".//w:p"]))
                    continue
                if name.startswith("word/footer"):
                    footer_parts.extend(_extract_docx_xml_text(xml_bytes, [".//w:p"]))
                    continue
                if name == "word/comments.xml":
                    comment_parts.extend(_extract_docx_xml_text(xml_bytes, [".//w:comment"]))
                    continue
                if name == "word/document.xml":
                    revision_parts.extend(
                        _extract_docx_xml_text(
                            xml_bytes,
                            [".//w:ins", ".//w:del"],
                        )
                    )
                    text_box_parts.extend(
                        _extract_docx_xml_text(
                            xml_bytes,
                            [".//w:txbxContent"],
                        )
                    )

            if header_parts:
                sections.append("[Headers]\n" + "\n".join(header_parts))
            if footer_parts:
                sections.append("[Footers]\n" + "\n".join(footer_parts))
            if comment_parts:
                sections.append("[Comments]\n" + "\n".join(comment_parts))
            if revision_parts:
                sections.append("[Tracked Changes]\n" + "\n".join(revision_parts))
            if text_box_parts:
                sections.append("[Text Boxes]\n" + "\n".join(text_box_parts))
    except zipfile.BadZipFile:
        return []

    return sections


def _extract_docx_sync(content: bytes) -> str:
    """
    Extrait le texte d'un DOCX via python-docx.
    • Paragraphes et tableaux dans l'ordre du document
    • Cellules d'une même ligne jointes par " | "
    • Cellules fusionnées dédupliquées (via _tc identity)
    Lève ValueError pour les fichiers corrompus ou invalides.
    """
    check_zip_safety(content)
    buf = io.BytesIO(content)
    try:
        document = DocxDocument(buf)
    except Exception as exc:
        raise ValueError(
            f"Fichier DOCX invalide ou corrompu : {type(exc).__name__}"
        ) from exc

    parts: list[str] = []

    for kind, block in _iter_docx_blocks(document):
        if kind == "p":
            text = block.text.strip()
            if text:
                parts.append(text)

        else:  # kind == "tbl"
            for row in block.rows:
                # Déduplique les cellules fusionnées par identité de l'élément XML
                seen_tc: set = set()
                cells: list[str] = []
                for cell in row.cells:
                    tc_id = id(cell._tc)
                    if tc_id in seen_tc:
                        continue
                    seen_tc.add(tc_id)
                    cell_text = cell.text.strip()
                    if cell_text:
                        cells.append(cell_text)
                if cells:
                    parts.append(" | ".join(cells))

    parts.extend(_extract_docx_structured_sections(content))

    if not parts:
        raise ValueError(
            "Le fichier DOCX ne contient aucun texte extractible."
        )
    return "\n".join(parts)


def _extract_txt_sync(content: bytes) -> str:
    """
    Décode un fichier texte brut en UTF-8 (fallback Latin-1).
    Lève ValueError si le contenu est vide après décodage.
    """
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    stripped = text.strip()
    if not stripped:
        raise ValueError("Le fichier TXT ne contient aucun texte.")
    return stripped


def _decode_text_content(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("latin-1", errors="replace")


def _extract_rtf_sync(content: bytes) -> str:
    text = _decode_text_content(content)
    text = _re.sub(r"\\'[0-9a-fA-F]{2}", " ", text)
    text = _re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", text)
    text = _re.sub(r"[{}]", " ", text)
    text = _re.sub(r"\s+", " ", text).strip()
    if not text:
        raise ValueError("Le fichier RTF ne contient aucun texte extractible.")
    return text


def _extract_vtt_sync(file_bytes: bytes) -> str:
    content = _decode_text_content(file_bytes)
    lines: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "WEBVTT" or "-->" in stripped or stripped.isdigit():
            continue
        lines.append(stripped)

    text = "\n".join(lines).strip()
    if not text:
        raise ValueError("Le transcript ne contient aucun texte extractible.")
    return text


def _extract_html_sync(file_bytes: bytes) -> str:
    try:
        from bs4 import BeautifulSoup  # noqa: PLC0415
    except ImportError as exc:
        raise ValueError("beautifulsoup4 requis pour les fichiers HTML (pip install beautifulsoup4)") from exc

    soup = BeautifulSoup(file_bytes, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()

    text = soup.get_text(separator="\n", strip=True)
    if not text:
        raise ValueError("Le fichier HTML ne contient aucun texte extractible.")
    return text


def _extract_yaml_sync(content: bytes) -> str:
    try:
        import yaml  # noqa: PLC0415
    except ImportError as exc:
        raise ValueError("PyYAML requis pour les fichiers YAML (pip install pyyaml)") from exc

    raw = _decode_text_content(content).strip()
    if not raw:
        raise ValueError("Le fichier YAML ne contient aucun texte extractible.")
    try:
        data = yaml.safe_load(raw)
    except Exception:
        return raw
    return raw if data is None else yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def _extract_json_sync(content: bytes) -> str:
    text = _decode_text_content(content).strip()
    if not text:
        raise ValueError("Le fichier JSON ne contient aucun texte extractible.")
    return text


def _extract_xml_sync(content: bytes) -> str:
    raw_text = _decode_text_content(content).strip()
    if not raw_text:
        raise ValueError("Le fichier XML ne contient aucun texte extractible.")

    try:
        root = ET.fromstring(raw_text)
    except ET.ParseError:
        return raw_text

    parts = [text.strip() for text in root.itertext() if text and text.strip()]
    return "\n".join(parts) if parts else raw_text


def _extract_odf_sync(content: bytes, label: str) -> str:
    check_zip_safety(content)
    _warn_if_zip_contains_macros(content, label)
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            try:
                xml_bytes = archive.read("content.xml")
            except KeyError as exc:
                raise ValueError(f"Le fichier {label} ne contient pas content.xml.") from exc
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Fichier {label} invalide ou corrompu : BadZipFile") from exc

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"Le fichier {label} contient un XML invalide.") from exc

    parts = [text.strip() for text in root.itertext() if text and text.strip()]
    text = "\n".join(parts).strip()
    if not text:
        raise ValueError(f"Le fichier {label} ne contient aucun texte extractible.")
    return text


def _extract_odt_sync(content: bytes) -> str:
    return _extract_odf_sync(content, "ODT")


def _extract_ods_sync(content: bytes) -> str:
    return _extract_odf_sync(content, "ODS")


def _extract_odp_sync(content: bytes) -> str:
    return _extract_odf_sync(content, "ODP")


def _extract_legacy_office_sync(content: bytes, label: str) -> str:
    text = _decode_text_content(content).replace("\x00", " ")
    text = _re.sub(r"[\x00-\x08\x0b-\x1f]", " ", text)
    text = _re.sub(r"\s+", " ", text).strip()
    if len(text) < 32:
        raise ValueError(f"Le fichier {label} ne contient aucun texte extractible.")
    return text


def _extract_doc_sync(content: bytes) -> str:
    if zipfile.is_zipfile(io.BytesIO(content)):
        return _extract_docx_sync(content)
    return _extract_legacy_office_sync(content, "DOC")


def _extract_ppt_sync(content: bytes) -> str:
    if zipfile.is_zipfile(io.BytesIO(content)):
        return _extract_pptx_sync(content)
    return _extract_legacy_office_sync(content, "PPT")


def _extract_msg_sync(content: bytes) -> str:
    return _extract_legacy_office_sync(content, "MSG")


def _extract_tsv_sync(content: bytes) -> str:
    return _extract_csv_like_sync(content, delimiter="\t", label="TSV")


def _detect_extension(filename: str) -> str:
    """Retourne l'extension en minuscules (sans point). Lève ValueError si absente."""
    if "." not in filename:
        raise ValueError(
            f"Impossible de détecter le type du fichier : '{filename}' "
            "n'a pas d'extension."
        )
    return filename.rsplit(".", 1)[-1].lower()


def _truncate_extracted_text(text: str) -> str:
    if len(text) <= _MAX_EXTRACTED_CHARS:
        return text
    head = text[:35_000].rstrip()
    tail = text[-15_000:].lstrip()
    omitted = len(text) - len(head) - len(tail)
    return (
        f"{head}\n\n[... contenu tronque intelligemment : {omitted} caracteres omis ...]\n\n{tail}"
    )




# ---------------------------------------------------------------------------
# Colonnes sensibles (xlsx / csv)
# ---------------------------------------------------------------------------

_SENSITIVE_HEADERS: frozenset[str] = frozenset({
    "nom", "prenom", "prénom", "email", "e-mail", "mail", "courriel",
    "iban", "rib", "telephone", "téléphone", "tel", "tél", "mobile",
    "salaire", "remuneration", "rémunération", "revenu", "brut", "net",
    "nir", "securite sociale", "sécurité sociale", "num secu", "n° secu",
    "numero secu", "numéro sécu", "ss",
})


def _is_sensitive_header(header: str) -> bool:
    return header.strip().lower() in _SENSITIVE_HEADERS


# ---------------------------------------------------------------------------
# Extracteurs synchrones — nouveaux formats
# ---------------------------------------------------------------------------

def _extract_xlsx_sync(content: bytes) -> str:
    """
    Extrait le texte d'un fichier .xlsx via openpyxl.
    Detecte les colonnes sensibles par nom de colonne.
    Retourne une representation textuelle tabulaire.
    """
    check_zip_safety(content)
    _warn_if_zip_contains_macros(content, "XLSX")
    try:
        import openpyxl  # noqa: PLC0415
    except ImportError as exc:
        raise ValueError("openpyxl requis pour les fichiers .xlsx (pip install openpyxl)") from exc

    buf = io.BytesIO(content)
    try:
        wb = openpyxl.load_workbook(buf, read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError(f"Fichier XLSX invalide ou corrompu : {type(exc).__name__}") from exc

    parts: list[str] = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue

        parts.append(f"[Feuille: {sheet_name}]")

        headers = [str(h).strip() if h is not None else "" for h in rows[0]]
        sensitive = [h for h in headers if _is_sensitive_header(h)]
        if sensitive:
            parts.append(f"[Colonnes sensibles: {', '.join(sensitive)}]")

        parts.append(" | ".join(headers))

        for row in rows[1:]:
            cells = [str(c) if c is not None else "" for c in row]
            if any(cells):
                parts.append(" | ".join(cells))

    wb.close()
    text = "\n".join(parts)
    if not text.strip():
        raise ValueError("Le fichier XLSX ne contient aucune donnee extractible.")
    return text


def _extract_xls_sync(content: bytes) -> str:
    """
    Extrait le texte d'un fichier .xls (Excel 97-2003) via xlrd.
    """
    try:
        import xlrd  # noqa: PLC0415
    except ImportError as exc:
        raise ValueError("xlrd requis pour les fichiers .xls (pip install xlrd)") from exc

    try:
        wb = xlrd.open_workbook(file_contents=content)
    except Exception as exc:
        raise ValueError(f"Fichier XLS invalide ou corrompu : {type(exc).__name__}") from exc

    parts: list[str] = []
    for sheet_name in wb.sheet_names():
        ws = wb.sheet_by_name(sheet_name)
        if ws.nrows == 0:
            continue

        parts.append(f"[Feuille: {sheet_name}]")

        headers = [str(ws.cell_value(0, c)).strip() for c in range(ws.ncols)]
        sensitive = [h for h in headers if _is_sensitive_header(h)]
        if sensitive:
            parts.append(f"[Colonnes sensibles: {', '.join(sensitive)}]")

        parts.append(" | ".join(headers))

        for r in range(1, ws.nrows):
            cells = [str(ws.cell_value(r, c)) for c in range(ws.ncols)]
            if any(cells):
                parts.append(" | ".join(cells))

    text = "\n".join(parts)
    if not text.strip():
        raise ValueError("Le fichier XLS ne contient aucune donnee extractible.")
    return text


def _extract_pptx_sync(content: bytes) -> str:
    """
    Extrait le texte d'un fichier .pptx en preservant la structure des slides,
    notes, tableaux et en marquant les slides masquees.
    """
    check_zip_safety(content)
    _warn_if_zip_contains_macros(content, "PPTX")
    try:
        from pptx import Presentation  # noqa: PLC0415
    except ImportError as exc:
        raise ValueError("python-pptx requis pour les fichiers .pptx (pip install python-pptx)") from exc

    try:
        presentation = Presentation(io.BytesIO(content))
    except Exception as exc:
        raise ValueError(f"Fichier PPTX invalide ou corrompu : {type(exc).__name__}") from exc

    parts: list[str] = []
    for index, slide in enumerate(presentation.slides, start=1):
        slide_parts: list[str] = [f"[Slide {index}]"]
        if str(slide._element.get("show", "1")).lower() in {"0", "false"}:
            slide_parts.append("[SLIDE MASQUÉE]")

        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                text = shape.text.strip()
                if text:
                    slide_parts.append(text)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [cell.text.strip() for cell in row.cells if cell.text and cell.text.strip()]
                    if cells:
                        slide_parts.append(" | ".join(cells))

        try:
            notes_frame = slide.notes_slide.notes_text_frame
        except Exception:
            notes_frame = None
        if notes_frame is not None:
            notes_text = notes_frame.text.strip()
            if notes_text:
                slide_parts.append("[Speaker Notes]")
                slide_parts.append(notes_text)

        comments = getattr(slide, "comments", None)
        if comments:
            comment_texts = []
            for comment in comments:
                text = getattr(comment, "text", "")
                if text and text.strip():
                    comment_texts.append(text.strip())
            if comment_texts:
                slide_parts.append("[Comments]")
                slide_parts.extend(comment_texts)

        if len(slide_parts) > 1:
            parts.append("\n".join(slide_parts))

    text = "\n\n".join(parts)
    if not text.strip():
        raise ValueError("Le fichier PPTX ne contient aucun texte extractible.")
    return text


def _extract_csv_sync(content: bytes) -> str:
    """
    Extrait et structure le texte d'un fichier CSV.
    Detecte les colonnes sensibles par nom de header.
    """
    import csv as _csv  # noqa: PLC0415

    try:
        text_raw = content.decode("utf-8")
    except UnicodeDecodeError:
        text_raw = content.decode("latin-1", errors="replace")

    dialect = _csv.Sniffer().sniff(text_raw[:4096]) if text_raw else _csv.excel
    reader = _csv.reader(io.StringIO(text_raw), dialect)
    rows = list(reader)

    if not rows:
        raise ValueError("Le fichier CSV est vide.")

    headers = [h.strip() for h in rows[0]]
    sensitive = [h for h in headers if _is_sensitive_header(h)]

    parts: list[str] = ["[CSV]"]
    if sensitive:
        parts.append(f"[Colonnes sensibles: {', '.join(sensitive)}]")

    for row in rows:
        if row:
            parts.append(" | ".join(row))

    return "\n".join(parts)


def _extract_image_sync(content: bytes) -> str:
    """
    Extrait le texte d'une image via OCR pytesseract + Pillow.
    """
    if not settings.OCR_ENABLED:
        return "[OCR non disponible — image non traitée]"

    try:
        from PIL import Image  # noqa: PLC0415
        import pytesseract       # noqa: PLC0415
    except ImportError as exc:
        raise ValueError(
            "pytesseract et Pillow requis pour les images "
            "(pip install pytesseract Pillow)"
        ) from exc

    buf = io.BytesIO(content)
    try:
        img = Image.open(buf)
        img.load()
    except Exception as exc:
        raise ValueError(f"Image invalide ou corrompue : {type(exc).__name__}") from exc

    try:
        text = pytesseract.image_to_string(img, lang="fra+eng")
    except Exception:
        text = pytesseract.image_to_string(img)

    stripped = text.strip()
    if not stripped:
        raise ValueError("L'image ne contient aucun texte extractible par OCR.")
    return stripped


def _extract_eml_sync(content: bytes) -> str:
    """
    Extrait le corps et les en-tetes d'un fichier .eml (email RFC 822).
    Inclut les pieces jointes textuelles.
    """
    import email as _email          # noqa: PLC0415
    from email import policy as _ep  # noqa: PLC0415
    import re as _re                 # noqa: PLC0415

    try:
        msg = _email.message_from_bytes(content, policy=_ep.default)
    except Exception as exc:
        raise ValueError(f"Fichier EML invalide : {type(exc).__name__}") from exc

    parts: list[str] = []

    for header in ("Subject", "From", "To", "Cc", "Date"):
        val = msg.get(header, "")
        if val:
            parts.append(f"{header}: {val}")

    for part in msg.walk():
        ct = part.get_content_type()
        cd = str(part.get("Content-Disposition", ""))

        if ct == "text/plain" and "attachment" not in cd:
            try:
                body = part.get_content()
                if body and body.strip():
                    parts.append(body.strip())
            except Exception:
                pass
        elif ct == "text/html" and "attachment" not in cd:
            try:
                html = part.get_content()
                if html:
                    clean = _re.sub(r"<[^>]+>", " ", html)
                    clean = _re.sub(r"\s+", " ", clean).strip()
                    if clean:
                        parts.append(clean)
            except Exception:
                pass
        elif "attachment" in cd and ct == "text/plain":
            try:
                att = part.get_content()
                fn = part.get_filename("piece-jointe")
                if att and att.strip():
                    parts.append(f"[Piece jointe: {fn}]\n{att.strip()}")
            except Exception:
                pass

    if not parts:
        raise ValueError("L'email ne contient aucun texte extractible.")
    return "\n\n".join(parts)

# ---------------------------------------------------------------------------
# ② Fonctions publiques
# ---------------------------------------------------------------------------

async def extract_text(file_bytes: bytes, filename: str) -> str:
    """
    Détecte automatiquement le type du fichier via son extension,
    puis extrait le texte entièrement en mémoire (aucun fichier temporaire).

    Args:
        file_bytes : contenu brut du fichier
        filename   : nom du fichier (utilisé pour détecter l'extension)

    Returns:
        Texte brut du document (str), jamais vide.

    Raises:
        ValueError  : extension non supportée, fichier corrompu ou vide
        HTTPException(413) : fichier trop volumineux (> _MAX_BYTES)
    """
    # ── Vérification de taille (avant toute extraction) ──────────────────────
    size = len(file_bytes)
    if size == 0:
        raise ValueError("Le fichier est vide (0 octet).")

    max_bytes = min(_MAX_BYTES, settings.MAX_FILE_SIZE_MB * 1024 * 1024)
    if size > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"Fichier trop volumineux ({size / 1_048_576:.1f} Mo). "
                f"Taille maximale autorisée : {max_bytes // 1_048_576} Mo."
            ),
        )

    ext = _detect_extension(filename)

    _extractors: dict[str, Callable[[bytes], str]] = {
        "pdf": _extract_pdf_sync,
        "docx": _extract_docx_sync,
        "doc": _extract_doc_sync,
        "odt": _extract_odt_sync,
        "txt": _extract_txt_sync,
        "md": _extract_txt_sync,
        "rtf": _extract_rtf_sync,
        "xlsx": _extract_xlsx_sync,
        "xls": _extract_xls_sync,
        "ods": _extract_ods_sync,
        "pptx": _extract_pptx_sync,
        "ppt": _extract_ppt_sync,
        "odp": _extract_odp_sync,
        "csv": _extract_csv_sync,
        "tsv": _extract_tsv_sync,
        "jpg": _extract_image_sync,
        "jpeg": _extract_image_sync,
        "png": _extract_image_sync,
        "gif": _extract_image_sync,
        "webp": _extract_image_sync,
        "bmp": _extract_image_sync,
        "tiff": _extract_image_sync,
        "tif": _extract_image_sync,
        "eml": _extract_eml_sync,
        "msg": _extract_msg_sync,
        "vtt": _extract_vtt_sync,
        "srt": _extract_vtt_sync,
        "html": _extract_html_sync,
        "htm": _extract_html_sync,
        "json": _extract_json_sync,
        "xml": _extract_xml_sync,
        "yaml": _extract_yaml_sync,
        "yml": _extract_yaml_sync,
    }
    extractor = _extractors.get(ext)
    if extractor is None:
        raise ValueError(
            f"Type de fichier non supporté : {ext!r}. "
            f"Types acceptés : {sorted(_extractors)}."
        )

    logger.info(
        "extract_text.start | ext=%s | size=%d",
        ext, size,
    )

    # Extraction dans le thread pool (fitz / python-docx sont synchrones)
    loop = asyncio.get_running_loop()
    text: str = await loop.run_in_executor(_executor, extractor, file_bytes)
    text = _truncate_extracted_text(text)

    logger.info(
        "extract_text.done | ext=%s | chars=%d",
        ext, len(text),
    )
    return text


def chunk_text(
    text: str,
    chunk_size: int = 2000,
    overlap: int = 200,
) -> list[str]:
    """
    Découpe *text* en chunks chevauchants pour ne pas couper les entités nommées.

    Stratégie de point de coupe (priorité décroissante) :
      1. Double saut de ligne (\\n\\n) — séparation de paragraphe
      2. Fin de phrase (". ", "! ", "? ", ".\\n", "!\\n", "?\\n")
      3. Saut de ligne simple (\\n)
      4. Espace (limite de mot)
      5. Hard split à chunk_size (aucun séparateur trouvé)

    Le chevauchement de `overlap` caractères en début de chaque chunk garantit
    qu'une entité nommée en limite de chunk est entièrement incluse dans au moins
    l'un des deux chunks adjacents.

    Args:
        text       : texte brut à découper
        chunk_size : taille cible de chaque chunk en caractères (défaut 2000)
        overlap    : chevauchement entre chunks successifs (défaut 200)

    Returns:
        Liste de chunks non vides, sans transformation du texte.
        Si len(text) ≤ chunk_size → [text] (aucun découpage).

    Raises:
        ValueError : si overlap ≥ chunk_size
    """
    if not text or not text.strip():
        return []

    if overlap >= chunk_size:
        raise ValueError(
            f"overlap ({overlap}) doit être strictement inférieur à chunk_size ({chunk_size})."
        )

    cleaned = text.strip()
    if len(cleaned) <= chunk_size:
        return [cleaned]

    chunks: list[str] = []
    length = len(cleaned)
    start = 0

    while start < length:
        end = min(start + chunk_size, length)

        if end < length:
            split = _find_split_point(cleaned, start, end)
        else:
            split = end

        chunk = cleaned[start:split].strip()
        if chunk:
            chunks.append(chunk)

        if split >= length:
            break

        # Prochain chunk : recule de `overlap` caractères (overlap garanti)
        next_start = max(start + 1, split - overlap)
        start = next_start

    return [c for c in chunks if c.strip()]


async def process_file(
    file_bytes: bytes,
    filename: str,
    redactor: "Redactor",
    vault: "Vault",
    session_id: str,
    user_id: str = "",
    entities: Optional[List[str]] = None,
    min_confidence_score: float = 0.6,
    excluded_entity_types: Optional[List[str]] = None,
    chunk_size: int = 2000,
    overlap: int = 200,
) -> dict:
    """
    Pipeline complet : extraction → chunking → pseudonymisation → stockage vault.

    Chaque chunk est pseudonymisé indépendamment. Les mappings sont fusionnés
    dans le vault sous *session_id* pour permettre la ré-identification ultérieure.

    Args:
        file_bytes : contenu brut du fichier
        filename   : nom du fichier (extension obligatoire)
        redactor   : instance Redactor (smart_pseudonymize)
        vault      : instance Vault (store_mapping)
        session_id : identifiant de session vault cible
        chunk_size : taille cible des chunks (défaut 2000 chars)
        overlap    : chevauchement inter-chunks (défaut 200 chars)

    Returns:
        {
            "session_id"    : str   — identifiant de session vault
            "chunks_count"  : int   — nombre de chunks traités
            "total_entities": int   — total d'entités PII pseudonymisées
            "token_estimate": int   — estimation de tokens LLM (chars / 4)
            "original_chars": int   — longueur du texte extrait
            "redacted_text" : str   — texte pseudonymisé complet (chunks concaténés)
        }

    Raises:
        HTTPException(400)  : fichier vide, extension invalide
        HTTPException(413)  : fichier trop volumineux (> 10 Mo)
        HTTPException(422)  : fichier corrompu ou sans texte extractible
        HTTPException(500)  : erreur inattendue pendant la pseudonymisation
    """
    # ── Extraction ────────────────────────────────────────────────────────────
    try:
        text = await extract_text(file_bytes, filename)
    except HTTPException:
        raise  # re-propagate size / format errors as-is
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.error(
            "process_file.extract ERREUR | session=%s | %s",
            _h(session_id), type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Extraction échouée : {type(exc).__name__}.",
        ) from exc

    # ── Chunking ──────────────────────────────────────────────────────────────
    chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
    if not chunks:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Le fichier ne contient aucun texte extractible après nettoyage.",
        )

    logger.info(
        "process_file.chunks | session=%s | chunks=%d | chars=%d",
        _h(session_id), len(chunks), len(text),
    )

    # ── Pseudonymisation de chaque chunk + stockage vault ────────────────────
    pseudonymised_chunks: list[str] = []
    total_entities = 0

    for i, chunk in enumerate(chunks):
        try:
            pseudo_chunk, mapping = await redactor.smart_pseudonymize(
                chunk,
                entities=entities,
                score_threshold=min_confidence_score,
                excluded_entity_types=excluded_entity_types,
            )
        except Exception as exc:
            logger.error(
                "process_file.redact ERREUR | session=%s | chunk=%d | %s",
                _h(session_id), i, type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Pseudonymisation échouée sur le chunk {i} : {type(exc).__name__}.",
            ) from exc

        if mapping:
            await vault.store_mapping(session_id, mapping, user_id=user_id)
            total_entities += len(mapping)

        pseudonymised_chunks.append(pseudo_chunk)

    # Estimation tokens : ~ 1 token / 4 chars (indépendant de la langue)
    token_estimate = max(1, len(text) // _CHARS_PER_TOKEN)

    logger.info(
        "process_file.done | session=%s | chunks=%d | entities=%d | tokens_est=%d",
        _h(session_id), len(chunks), total_entities, token_estimate,
    )

    return {
        "session_id":     session_id,
        "chunks_count":   len(chunks),
        "total_entities": total_entities,
        "token_estimate": token_estimate,
        "original_chars": len(text),
        "redacted_text":  "\n\n".join(pseudonymised_chunks),
    }


# ---------------------------------------------------------------------------
# Backward-compat — classe utilisée par les routes legacy (api/routes/proxy.py)
# ---------------------------------------------------------------------------

class FileProcessor:
    """
    Wrapper synchrone autour des extracteurs, maintenu pour compatibilité
    avec les routes legacy. Préférer les fonctions module-level pour le nouveau code.
    """

    def __init__(self) -> None:
        self._extractors: Dict[str, Callable[[bytes], str]] = {
            "pdf":  _extract_pdf_sync,
            "docx": _extract_docx_sync,
            "odt": _extract_odt_sync,
            "pptx": _extract_pptx_sync,
            "odp": _extract_odp_sync,
            "xlsx": _extract_xlsx_sync,
            "xls": _extract_xls_sync,
            "ods": _extract_ods_sync,
            "csv": _extract_csv_sync,
            "tsv": _extract_tsv_sync,
            "html": _extract_html_sync,
            "htm": _extract_html_sync,
            "json": _extract_json_sync,
            "xml": _extract_xml_sync,
            "yaml": _extract_yaml_sync,
            "yml": _extract_yaml_sync,
            "md": _extract_txt_sync,
            "rtf": _extract_rtf_sync,
            "vtt": _extract_vtt_sync,
            "srt": _extract_vtt_sync,
            "txt":  self._extract_txt,
        }

    def extract(self, content: bytes, extension: str) -> str:
        """Extrait le texte depuis des bytes. Lève ValueError si extension inconnue."""
        ext = extension.lower().lstrip(".")
        extractor = self._extractors.get(ext)
        if extractor is None:
            raise ValueError(f"Type de fichier non supporté : {ext!r}.")
        text = extractor(content)
        logger.info("FileProcessor.extract | ext=%s | chars=%d", ext, len(text))
        return text

    @staticmethod
    def _extract_txt(content: bytes) -> str:
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            return content.decode("latin-1", errors="replace")


# ---------------------------------------------------------------------------
# Helper — point de coupe pour chunk_text
# ---------------------------------------------------------------------------

def _find_split_point(text: str, start: int, end: int) -> int:
    """
    Trouve le meilleur point de coupe dans text[start:end] en regardant
    à rebours depuis *end* sur une fenêtre de 150 chars maximum.

    Priorité :
      1. Paragraphe (\\n\\n)
      2. Fin de phrase + espace (". ", "! ", "? ")
      3. Fin de phrase + newline (".\\n", "!\\n", "?\\n")
      4. Saut de ligne simple (\\n)
      5. Espace (limite de mot)
      6. Retour de *end* (hard split)
    """
    look_from = max(start + 1, end - 150)

    # Paragraphe
    pos = text.rfind("\n\n", look_from, end)
    if pos > start:
        return pos + 2

    # Fin de phrase avec espace
    for punct in (". ", "! ", "? "):
        pos = text.rfind(punct, look_from, end)
        if pos > start:
            return pos + len(punct)

    # Fin de phrase avec newline
    for punct in (".\n", "!\n", "?\n"):
        pos = text.rfind(punct, look_from, end)
        if pos > start:
            return pos + len(punct)

    # Newline simple
    pos = text.rfind("\n", look_from, end)
    if pos > start:
        return pos + 1

    # Espace (limite de mot)
    pos = text.rfind(" ", look_from, end)
    if pos > start:
        return pos + 1

    # Hard split (aucun séparateur trouvé dans la fenêtre)
    return end


# ---------------------------------------------------------------------------
# Patterns de detection PII par contenu (colonnes Excel)
# ---------------------------------------------------------------------------

_RE_EMAIL = _re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_RE_PHONE = _re.compile(r"(?:\+33|0)[1-9](?:[\s\-.]?\d{2}){4}")
_RE_IBAN  = _re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[\dA-Z]{4}){4,7}\b")
_RE_NIR   = _re.compile(r"\b[12]\d{14}\b")

_CONTENT_PATTERNS: tuple = (_RE_EMAIL, _RE_PHONE, _RE_IBAN, _RE_NIR)


def _is_sensitive_by_content(values: list) -> bool:
    """
    Retourne True si >= 20 % des valeurs non vides d'une colonne correspondent
    a un pattern PII connu (email, telephone, IBAN, NIR).
    Utilisee comme detection de secours quand le nom de colonne n'est pas reconnu.
    """
    non_empty = [v for v in values if v.strip()]
    if not non_empty:
        return False
    matches = sum(1 for v in non_empty if any(p.search(v) for p in _CONTENT_PATTERNS))
    return matches / len(non_empty) >= 0.2


class ExcelProcessResult(TypedDict):
    markdown: str
    workbook_bytes: bytes
    mapping: dict
    sensitive_cols: list[str]
    entities_count: int
    sheets_count: int
    report: dict
    excel_controls: Optional[dict]


def _convert_xls_to_xlsx(file_bytes: bytes) -> bytes:
    try:
        import xlrd  # noqa: PLC0415
    except ImportError as exc:
        raise ValueError("xlrd requis pour les fichiers .xls (pip install xlrd)") from exc

    try:
        from openpyxl import Workbook  # noqa: PLC0415
    except ImportError as exc:
        raise ValueError("openpyxl requis pour convertir les fichiers .xls") from exc

    try:
        xls_book = xlrd.open_workbook(file_contents=file_bytes)
    except Exception as exc:
        raise ValueError(f"Fichier XLS invalide ou corrompu : {type(exc).__name__}") from exc

    workbook = Workbook()
    workbook.remove(workbook.active)

    for sheet_name in xls_book.sheet_names():
        source_sheet = xls_book.sheet_by_name(sheet_name)
        target_sheet = workbook.create_sheet(sheet_name)
        for row_index in range(source_sheet.nrows):
            for col_index in range(source_sheet.ncols):
                target_sheet.cell(
                    row=row_index + 1,
                    column=col_index + 1,
                    value=source_sheet.cell_value(row_index, col_index),
                )

    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _load_workbook_rw(file_bytes: bytes):
    try:
        import openpyxl  # noqa: PLC0415
    except ImportError as exc:
        raise ValueError("openpyxl requis (pip install openpyxl)") from exc

    buf = io.BytesIO(file_bytes)
    try:
        return openpyxl.load_workbook(buf, keep_vba=False, data_only=True)
    except Exception as exc:
        raise ValueError(f"Fichier Excel invalide : {type(exc).__name__}") from exc


def _detect_sensitive_excel_columns(headers: list[str], data_rows: list) -> set[int]:
    sensitive_idx: set[int] = set()
    for col_i, header in enumerate(headers):
        if _is_sensitive_header(header):
            sensitive_idx.add(col_i)
            continue

        col_vals = []
        for row in data_rows:
            value = row[col_i].value if col_i < len(row) else None
            col_vals.append(str(value) if value is not None else "")
        if _is_sensitive_by_content(col_vals):
            sensitive_idx.add(col_i)
    return sensitive_idx


def _build_excel_controls_sync(file_bytes: bytes) -> dict:
    wb = _load_workbook_rw(file_bytes)
    try:
        from openpyxl.utils import get_column_letter  # noqa: PLC0415

        sheets: list[dict] = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            all_rows = list(ws.iter_rows())
            if not all_rows:
                sheets.append({"name": sheet_name, "sensitive_columns": []})
                continue

            headers = [
                str(cell.value).strip() if cell.value is not None else ""
                for cell in all_rows[0]
            ]
            sensitive_idx = _detect_sensitive_excel_columns(headers, all_rows[1:])
            sensitive_columns = []
            for col_i in sorted(sensitive_idx):
                letter = get_column_letter(col_i + 1)
                header = headers[col_i] if col_i < len(headers) else ""
                sensitive_columns.append(
                    {
                        "id": f"{sheet_name}!{letter}",
                        "letter": letter,
                        "header": header,
                        "label": header or letter,
                    }
                )
            sheets.append({"name": sheet_name, "sensitive_columns": sensitive_columns})
        return {"sheets": sheets}
    finally:
        wb.close()


def _normalize_token(value: str) -> str:
    return value.strip().lower()


def _parse_excluded_range(spec: str) -> Optional[tuple[Optional[str], tuple[int, Optional[int], int, Optional[int]]]]:
    try:
        from openpyxl.utils import column_index_from_string, range_boundaries  # noqa: PLC0415
    except ImportError as exc:
        raise ValueError("openpyxl requis (pip install openpyxl)") from exc

    raw = spec.strip()
    if not raw:
        return None

    sheet_name: Optional[str] = None
    if "!" in raw:
        sheet_name, raw = raw.split("!", 1)
        sheet_name = sheet_name.strip()

    raw = raw.strip().upper()
    if not raw:
        return None

    col_range_match = _re.fullmatch(r"([A-Z]+):([A-Z]+)", raw)
    if col_range_match:
        start_col = column_index_from_string(col_range_match.group(1))
        end_col = column_index_from_string(col_range_match.group(2))
        return sheet_name, (start_col, None, end_col, None)

    min_col, min_row, max_col, max_row = range_boundaries(raw)
    return sheet_name, (min_col, min_row, max_col, max_row)


def _is_cell_in_excluded_ranges(
    sheet_name: str,
    row_index: int,
    column_index: int,
    excluded_ranges: list[str],
) -> bool:
    for spec in excluded_ranges:
        parsed = _parse_excluded_range(spec)
        if parsed is None:
            continue
        target_sheet, boundaries = parsed
        min_col, min_row, max_col, max_row = boundaries
        if target_sheet and target_sheet != sheet_name:
            continue
        if not (min_col <= column_index <= max_col):
            continue
        if min_row is not None and row_index < min_row:
            continue
        if max_row is not None and row_index > max_row:
            continue
        return True
    return False


def _is_excel_column_excluded(
    sheet_name: str,
    column_letter: str,
    header: str,
    excluded_columns: list[str],
) -> bool:
    tokens = {
        _normalize_token(column_letter),
        _normalize_token(f"{sheet_name}!{column_letter}"),
    }
    if header:
        tokens.add(_normalize_token(header))
        tokens.add(_normalize_token(f"{sheet_name}!{header}"))
    return any(_normalize_token(item) in tokens for item in excluded_columns if item and item.strip())


# ---------------------------------------------------------------------------
# Excel -- pseudonymisation cellule par cellule (synchrone, thread pool)
# ---------------------------------------------------------------------------

def _process_excel_sync(
    file_bytes: bytes,
    excluded_sheets: Optional[list[str]] = None,
    excluded_columns: Optional[list[str]] = None,
    excluded_ranges: Optional[list[str]] = None,
) -> ExcelProcessResult:
    """
    Charge le workbook openpyxl, detecte les colonnes sensibles par :
      a) nom de colonne (_SENSITIVE_HEADERS)
      b) contenu des cellules (_CONTENT_PATTERNS, seuil 20 %)
    Pseudonymise les cellules sensibles avec des placeholders [HEADER_N] et
    retourne le workbook modifie (bytes) + le mapping vault.
    """
    from openpyxl.utils import get_column_letter  # noqa: PLC0415

    wb = _load_workbook_rw(file_bytes)
    excluded_sheets = excluded_sheets or []
    excluded_columns = excluded_columns or []
    excluded_ranges = excluded_ranges or []
    excluded_sheets_set = {_normalize_token(name) for name in excluded_sheets if name and name.strip()}

    all_mapping: dict = {}
    md_parts: list = []
    sensitive_cols_all: list = []
    _counter: dict = {}
    sheets_processed: list[str] = []
    sheets_excluded_report: list[str] = []
    columns_excluded_report: set[str] = set()
    columns_redacted_set: set[str] = set()
    excel_controls = _build_excel_controls_sync(file_bytes)

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        all_rows = list(ws.iter_rows())
        if not all_rows:
            continue

        if _normalize_token(sheet_name) in excluded_sheets_set:
            sheets_excluded_report.append(sheet_name)
            md_parts.append(f"\n## Feuille : {sheet_name}")
            headers = [
                str(cell.value).strip() if cell.value is not None else ""
                for cell in all_rows[0]
            ]
            if headers:
                md_parts.append("| " + " | ".join(headers) + " |")
                md_parts.append("| " + " | ".join(["---"] * len(headers)) + " |")
            for row in all_rows[1:]:
                cells = [str(cell.value).strip() if cell.value is not None else "" for cell in row[:len(headers)]]
                md_parts.append("| " + " | ".join(cells) + " |")
            continue

        headers = [
            str(cell.value).strip() if cell.value is not None else ""
            for cell in all_rows[0]
        ]
        n_cols = len(headers)
        data_rows = all_rows[1:]

        sensitive_idx = _detect_sensitive_excel_columns(headers, data_rows)
        sensitive_cols = [headers[i] for i in sorted(sensitive_idx) if i < len(headers)]
        sensitive_cols_all.extend(sensitive_cols)
        sheets_processed.append(sheet_name)

        # Markdown pour le LLM
        md_parts.append(f"\n## Feuille : {sheet_name}")
        if sensitive_cols:
            md_parts.append(
                f"*Colonnes sensibles pseudonymisees : {', '.join(sensitive_cols)}*\n"
            )
        if headers:
            md_parts.append("| " + " | ".join(headers) + " |")
            md_parts.append("| " + " | ".join(["---"] * n_cols) + " |")

        # Pseudonymisation des cellules sensibles
        for row_index, row in enumerate(data_rows, start=2):
            cells_out: list = []
            for col_i in range(n_cols):
                cell = row[col_i] if col_i < len(row) else None
                if cell is None:
                    cells_out.append("")
                    continue
                v = cell.value
                str_val = str(v).strip() if v is not None else ""
                col_letter = get_column_letter(col_i + 1)
                header = headers[col_i] if col_i < len(headers) else ""
                column_excluded = _is_excel_column_excluded(
                    sheet_name=sheet_name,
                    column_letter=col_letter,
                    header=header,
                    excluded_columns=excluded_columns,
                )
                cell_excluded = _is_cell_in_excluded_ranges(
                    sheet_name=sheet_name,
                    row_index=row_index,
                    column_index=col_i + 1,
                    excluded_ranges=excluded_ranges,
                )

                if column_excluded:
                    columns_excluded_report.add(f"{sheet_name}!{col_letter}")

                if col_i in sensitive_idx and str_val and not column_excluded and not cell_excluded:
                    # Placeholder unique par colonne : [COLONNE_N]
                    key = header.upper().replace(" ", "_")[:20] or "VAL"
                    _counter[key] = _counter.get(key, 0) + 1
                    placeholder = f"[{key}_{_counter[key]}]"
                    all_mapping[placeholder] = str_val
                    cell.value = placeholder   # modifie le workbook en place
                    cells_out.append(placeholder)
                    columns_redacted_set.add(header)
                else:
                    cells_out.append(str_val)

            md_parts.append("| " + " | ".join(cells_out) + " |")

    # Serialiser le workbook pseudonymise
    out_buf = io.BytesIO()
    wb.save(out_buf)

    return {
        "markdown":       "\n".join(md_parts),
        "workbook_bytes": out_buf.getvalue(),
        "mapping":        all_mapping,
        "sensitive_cols": sensitive_cols_all,
        "entities_count": len(all_mapping),
        "sheets_count":   len(wb.sheetnames),
        "report": {
            "sheets_processed": sheets_processed,
            "sheets_excluded": sheets_excluded_report,
            "columns_redacted": sorted(columns_redacted_set),
            "columns_excluded": sorted(columns_excluded_report),
            "cells_redacted": len(all_mapping),
            "entities_by_type": dict(_counter),
        },
        "excel_controls": excel_controls,
    }


def _reidentify_excel_sync(workbook_bytes: bytes, mapping: dict) -> bytes:
    """
    Charge le workbook pseudonymise et remplace chaque placeholder par la
    valeur originale du vault. Retourne le workbook re-identifie en bytes.
    """
    try:
        import openpyxl  # noqa: PLC0415
    except ImportError as exc:
        raise ValueError("openpyxl requis (pip install openpyxl)") from exc

    buf = io.BytesIO(workbook_bytes)
    wb = openpyxl.load_workbook(buf)

    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if not isinstance(cell.value, str):
                    continue
                new_val = cell.value
                for placeholder, original in mapping.items():
                    if placeholder in new_val:
                        new_val = new_val.replace(placeholder, original)
                if new_val != cell.value:
                    cell.value = new_val

    out_buf = io.BytesIO()
    wb.save(out_buf)
    return out_buf.getvalue()


# ---------------------------------------------------------------------------
# process_excel -- pipeline async complet
# ---------------------------------------------------------------------------

async def process_excel(
    file_bytes: bytes,
    filename: str,
    user_id: str,
    session_id: str,
    vault: "Vault",
    excluded_sheets: Optional[list[str]] = None,
    excluded_columns: Optional[list[str]] = None,
    excluded_ranges: Optional[list[str]] = None,
) -> ExcelProcessResult:
    """
    Pipeline Excel : detection colonnes sensibles -> pseudonymisation cellule
    par cellule -> stockage vault -> markdown pour le LLM.

    La pseudonymisation est basee sur les en-tetes de colonnes et les patterns
    de contenu (email, telephone, IBAN, NIR). Elle est deterministe et cellule-
    atomique : chaque valeur sensible recoit un placeholder unique [HEADER_N].

    Args:
        file_bytes : contenu brut du fichier .xlsx
        filename   : nom du fichier (pour les logs)
        user_id    : proprietaire vault
        session_id : identifiant de session vault
        vault      : instance Vault (store_mapping)

    Returns:
        {
            "markdown"       : str   -- tableau pseudonymise (markdown) pour le LLM
            "workbook_bytes" : bytes -- workbook .xlsx avec placeholders (pour export)
            "sensitive_cols" : list  -- noms des colonnes sensibles detectees
            "entities_count" : int   -- nombre de cellules pseudonymisees
            "token_estimate" : int   -- estimation tokens (chars / 4)
        }
    """
    extension = _detect_extension(filename)
    working_bytes = file_bytes
    effective_excluded_sheets = excluded_sheets
    effective_excluded_columns = excluded_columns
    effective_excluded_ranges = excluded_ranges
    enable_excel_controls = True

    if extension == "xls":
        working_bytes = _convert_xls_to_xlsx(file_bytes)
        effective_excluded_sheets = []
        effective_excluded_columns = []
        effective_excluded_ranges = []
        enable_excel_controls = False

    check_zip_safety(working_bytes)
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        _executor,
        _process_excel_sync,
        working_bytes,
        effective_excluded_sheets,
        effective_excluded_columns,
        effective_excluded_ranges,
    )

    if result["mapping"]:
        await vault.store_mapping(session_id, result["mapping"], user_id=user_id)

    logger.info(
        "process_excel.done | session=%s | sheets=%d | entities=%d",
        _h(session_id), result["sheets_count"], result["entities_count"],
    )
    return {
        "markdown":       result["markdown"],
        "workbook_bytes": result["workbook_bytes"],
        "sensitive_cols": result["sensitive_cols"],
        "entities_count": result["entities_count"],
        "token_estimate": max(1, len(result["markdown"]) // _CHARS_PER_TOKEN),
        "report": result["report"],
        "excel_controls": result["excel_controls"] if enable_excel_controls else None,
        "sheets_count": result["sheets_count"],
    }


async def inspect_excel_controls(file_bytes: bytes, filename: str) -> Optional[dict]:
    if _detect_extension(filename) != "xlsx":
        return None
    check_zip_safety(file_bytes)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _build_excel_controls_sync, file_bytes)


async def export_excel_reidentified(
    workbook_bytes: bytes,
    vault: "Vault",
    user_id: str,
    session_id: str,
) -> bytes:
    """
    Recupere le mapping vault et re-identifie toutes les cellules du workbook.

    Args:
        workbook_bytes : workbook pseudonymise (.xlsx bytes)
        vault          : instance Vault (get_mapping)
        user_id        : proprietaire du vault
        session_id     : identifiant de session vault

    Returns:
        bytes du fichier .xlsx re-identifie

    Raises:
        ValueError : mapping vault introuvable ou workbook invalide
    """
    mapping = await vault.get_mapping(session_id, user_id=user_id)
    if not mapping:
        raise ValueError("Aucun mapping vault trouve pour cette session.")

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _executor, _reidentify_excel_sync, workbook_bytes, mapping
    )
