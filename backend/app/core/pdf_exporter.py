"""
Export PDF des conversations -- reeidentification des donnees reelles.

Contenu du PDF :
  - Page de garde : nom du projet, date, metadonnees
  - Corps : conversation re-identifiee (donnees reelles)
  - Annexe : mapping vault chiffre en base64 (pour reimport)

Securite :
  - Le mapping en annexe est chiffre avec VAULT_ENCRYPTION_KEY
  - Le PDF contient des donnees reelles -- avertissement RGPD dans l'UI
  - Logs : project_id uniquement -- zero contenu sensible

Reimport :
  - POST /projects/import extrait le bloc annexe, dechiffre, recrée le vault
"""

import base64
import io
import json
import re
from datetime import datetime, timezone
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.core.project_manager import decrypt_blob, encrypt_blob
from app.config import settings
from app.models.project import Project
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Marqueurs pour extraire le bloc annexe lors du reimport
_ANNEX_START = "=== VAULT_ANNEXE_START ==="
_ANNEX_END   = "=== VAULT_ANNEXE_END ==="


def _encrypt_mapping(mapping: dict) -> str:
    """Chiffre le mapping vault -> base64 (meme cle que le vault)."""
    master_key = settings.VAULT_ENCRYPTION_KEY.encode("utf-8")
    plaintext = json.dumps(mapping, ensure_ascii=False).encode("utf-8")
    blob = encrypt_blob(plaintext, master_key)
    return base64.b64encode(blob).decode("ascii")


def decrypt_mapping_b64(b64: str) -> dict:
    """Dechiffre un mapping vault depuis base64 (pour reimport)."""
    master_key = settings.VAULT_ENCRYPTION_KEY.encode("utf-8")
    blob = base64.b64decode(b64)
    plaintext = decrypt_blob(blob, master_key)
    return json.loads(plaintext.decode("utf-8"))


def extract_vault_from_pdf_bytes(pdf_bytes: bytes) -> Optional[str]:
    """
    Extrait le bloc annexe chiffre depuis les bytes d'un PDF.
    Utilise PyMuPDF pour lire le texte de toutes les pages.
    Retourne le base64 du mapping, ou None si introuvable.
    """
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        full_text = ""
        for page in doc:
            full_text += page.get_text()
        doc.close()
    except Exception as exc:
        logger.error("pdf.extract_vault: lecture PDF echouee | %s", type(exc).__name__)
        return None

    # Regex robuste : tolere les sauts de ligne dans le base64 (wrapping PDF)
    match = re.search(
        re.escape(_ANNEX_START) + r"\s*([A-Za-z0-9+/=\s]+?)\s*" + re.escape(_ANNEX_END),
        full_text,
        re.DOTALL,
    )
    if not match:
        logger.warning("pdf.extract_vault: bloc annexe introuvable")
        return None
    # Supprimer les espaces/sauts de ligne introduits par le rendu PDF
    return re.sub(r"\s+", "", match.group(1))


def export_conversation(project: Project, vault_mapping: Optional[dict] = None) -> bytes:
    """
    Genere un PDF complet pour le projet donne.
    Retourne les bytes du PDF.

    Structure :
      1. Page de garde
      2. Conversation re-identifiee (content_restored)
      3. Annexe avec mapping chiffre (pour reimport)
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2.5 * cm,
        bottomMargin=2 * cm,
    )

    styles = getSampleStyleSheet()
    style_normal = styles["Normal"]
    style_normal.fontSize = 10
    style_normal.leading = 14

    style_cover_title = ParagraphStyle(
        "CoverTitle",
        parent=styles["Heading1"],
        fontSize=22,
        spaceAfter=12,
        textColor=colors.HexColor("#1e3a5f"),
    )
    style_cover_sub = ParagraphStyle(
        "CoverSub",
        parent=styles["Normal"],
        fontSize=12,
        textColor=colors.HexColor("#555555"),
        spaceAfter=6,
    )
    style_warning = ParagraphStyle(
        "Warning",
        parent=styles["Normal"],
        fontSize=9,
        backColor=colors.HexColor("#fff3cd"),
        borderColor=colors.HexColor("#ffc107"),
        borderWidth=1,
        borderPadding=8,
        textColor=colors.HexColor("#856404"),
        spaceAfter=12,
    )
    style_h2 = ParagraphStyle(
        "H2",
        parent=styles["Heading2"],
        fontSize=13,
        textColor=colors.HexColor("#1e3a5f"),
        spaceBefore=16,
        spaceAfter=6,
    )
    style_user = ParagraphStyle(
        "UserMsg",
        parent=styles["Normal"],
        fontSize=10,
        leading=14,
        leftIndent=0,
        backColor=colors.HexColor("#eef2ff"),
        borderPadding=8,
        spaceAfter=8,
    )
    style_assistant = ParagraphStyle(
        "AssistantMsg",
        parent=styles["Normal"],
        fontSize=10,
        leading=14,
        leftIndent=20,
        backColor=colors.HexColor("#f0fdf4"),
        borderPadding=8,
        spaceAfter=8,
    )
    style_code = ParagraphStyle(
        "Code",
        parent=styles["Normal"],
        fontSize=7,
        fontName="Courier",
        leading=9,
        wordWrap="CJK",
    )

    story = []

    # ── Page de garde ─────────────────────────────────────────────────────
    story.append(Spacer(1, 3 * cm))
    story.append(Paragraph("Privacy Proxy", style_cover_sub))
    story.append(Paragraph(project.name, style_cover_title))

    meta_data = [
        ["Date d'export", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")],
        ["Projet cree le", project.created_at[:10]],
        ["Derniere activite", project.last_activity[:10]],
        ["Nombre d'echanges", str(project.messages_count)],
        ["Statut", project.status],
    ]
    meta_table = Table(meta_data, colWidths=[5 * cm, 10 * cm])
    meta_table.setStyle(
        TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#f8f9fa")]),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dee2e6")),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ])
    )
    story.append(Spacer(1, 1 * cm))
    story.append(meta_table)
    story.append(Spacer(1, 1 * cm))

    story.append(
        Paragraph(
            "AVERTISSEMENT RGPD : Ce document contient vos donnees personnelles reelles. "
            "Conservez-le en securite et ne le partagez pas. "
            "Le fichier annexe permet de reimporter la conversation dans Privacy Proxy.",
            style_warning,
        )
    )

    story.append(PageBreak())

    # ── Conversation re-identifiee ────────────────────────────────────────
    story.append(Paragraph("Conversation", style_h2))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#dee2e6")))
    story.append(Spacer(1, 0.3 * cm))

    for msg in project.messages:
        if msg.role == "user":
            label = "<b>Vous</b>"
            msg_style = style_user
        else:
            label = "<b>Assistant</b>"
            msg_style = style_assistant

        ts = msg.timestamp[:16].replace("T", " ") if msg.timestamp else ""
        header = f"{label}  <font size='8' color='#888888'>{ts}</font>"
        story.append(Paragraph(header, style_normal))

        # Escape HTML chars in content
        content = (
            msg.content_restored
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br/>")
        )
        story.append(Paragraph(content, msg_style))
        story.append(Spacer(1, 0.2 * cm))

    story.append(PageBreak())

    # ── Annexe : mapping chiffre ──────────────────────────────────────────
    story.append(Paragraph("Annexe technique : mapping de reimport", style_h2))
    story.append(
        Paragraph(
            "Le bloc ci-dessous contient le mapping de pseudonymisation chiffre "
            "(AES-256-GCM) necessaire pour reimporter cette conversation dans "
            "Privacy Proxy. Sans la cle de chiffrement du serveur, ce bloc est "
            "illisible.",
            style_normal,
        )
    )
    story.append(Spacer(1, 0.5 * cm))

    mapping: dict = vault_mapping or {}

    encrypted_mapping_b64 = _encrypt_mapping(mapping)

    annex_text = f"{_ANNEX_START}\n{encrypted_mapping_b64}\n{_ANNEX_END}"
    story.append(Paragraph(annex_text, style_code))

    doc.build(story)
    pdf_bytes = buf.getvalue()

    logger.info(
        "pdf.export | project=%s | pages=~ | size=%d bytes",
        project.project_id, len(pdf_bytes),
    )
    return pdf_bytes
