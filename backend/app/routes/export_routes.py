"""
Routes export multi-format et reimport.

GET  /projects/{project_id}/export       -- telecharge le PDF de la conversation
GET  /projects/{project_id}/export/docx  -- telecharge la conversation en DOCX
GET  /projects/{project_id}/export/txt   -- telecharge la conversation en TXT
GET  /projects/{project_id}/export/json  -- telecharge la conversation pseudonymisee en JSON
POST /projects/import                    -- reimporte depuis un export PDF/DOCX/TXT/JSON

Securite :
  - Le mapping en annexe PDF est chiffre avec VAULT_ENCRYPTION_KEY
  - Les exports PDF/DOCX/TXT contiennent des donnees re-identifiees
  - L'export JSON reste pseudonymise et ne contient pas content_restored
  - Logs : project_id uniquement -- zero contenu sensible
"""

import json
import re
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any, Callable

import magic as _magic
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import Response

from app.api.routes.auth import TokenData, get_current_user
from app.config import settings
from app.core.audit_trail import AuditEvent, get_audit_trail
from app.core.file_processor import extract_text
from app.core.pdf_exporter import (
    _ANNEX_END,
    _ANNEX_START,
    decrypt_mapping_b64,
    export_conversation,
    extract_vault_from_pdf_bytes,
)
from app.core.project_manager import ProjectManager
from app.core.redactor import get_shared_redactor
from app.core.vault import Vault
from app.middleware.rate_limit import limiter
from app.models.project import Project, ProjectMessage
from app.utils.logger import get_logger, hash_id as _h

logger = get_logger(__name__)
router = APIRouter()
_pm = ProjectManager()
_vault = Vault()

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_IMPORT_HEADER_PATTERNS = (
    re.compile(r"^\[(Utilisateur|Assistant)\]\s+(.+)$"),
    re.compile(r"^(Utilisateur|Assistant)\s+-\s+(.+)$"),
    re.compile(r"^(Vous|Assistant)\s+(\d{4}-\d{2}-\d{2}.*)$"),
)
_PLACEHOLDER_RE = re.compile(r"^\[([A-Z_]+)_(\d+)\]$")
_IMPORT_EXT_TO_MIME: dict[str, set[str]] = {
    "pdf": {"application/pdf"},
    "docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
    },
    "txt": {"text/plain"},
    "json": {"application/json", "text/plain"},
}


async def _safe_audit_log(event: AuditEvent) -> None:
    try:
        await get_audit_trail().log(event)
    except Exception as exc:
        logger.warning("export.audit WARN | %s", type(exc).__name__)


def _validate_project_id(project_id: str) -> None:
    if not _UUID_RE.match(project_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Format de project_id invalide.",
        )


async def _load_export_context(project_id: str, current_user: TokenData) -> tuple[str, bool, Project]:
    org_id = current_user.org_id or ""
    if not org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Compte non rattache a une organisation. Contactez votre administrateur.",
        )

    project = await _pm.get_project(project_id, current_user.username)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Projet introuvable ou acces refuse.",
        )

    try:
        from app.core.org_manager import OrgNotFoundError, get_org_manager  # noqa: PLC0415

        org_settings = await get_org_manager().get_settings(org_id)
        require_audit = getattr(
            getattr(org_settings, "policy", None),
            "require_audit_for_export",
            True,
        )
    except OrgNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organisation introuvable ou desactivee. Contactez votre administrateur.",
        ) from exc

    return org_id, require_audit, project


async def _log_export_event(
    *,
    current_user: TokenData,
    org_id: str,
    project_id: str,
    require_audit: bool,
    file_type: str,
    success: bool,
    error_code: str | None = None,
) -> None:
    event = AuditEvent.build(
        user_id=current_user.username,
        org_id=org_id,
        project_id=project_id,
        action="export",
        model_used="local",
        protection_mode="",
        entities_masked=0,
        file_type=file_type,
        cost_usd=0.0,
        success=success,
        error_code=error_code,
    )
    if require_audit:
        await get_audit_trail().log(event)
    else:
        await _safe_audit_log(event)


def _safe_filename(project: Project, extension: str) -> str:
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", project.name[:50])
    return f"privacy_proxy_{safe_name}.{extension}"


def _default_import_name(filename: str) -> str:
    stem = re.sub(r"\.[^.]+$", "", filename or "").strip()
    safe = re.sub(r"\s+", " ", stem)[:40].strip()
    if not safe:
        safe = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"[Import] {safe}"


def _detect_import_extension(filename: str, content: bytes) -> str:
    if not filename or "." not in filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nom de fichier invalide pour l'import.",
        )

    ext = filename.rsplit(".", 1)[-1].lower()
    if ext not in _IMPORT_EXT_TO_MIME:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Formats importes acceptes : PDF, DOCX, TXT, JSON.",
        )

    try:
        detected_mime = _magic.from_buffer(content[:2048], mime=True)
    except Exception as exc:
        logger.warning("import.magic_check WARN | %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Impossible de verifier le type du fichier importe.",
        ) from exc

    if detected_mime not in _IMPORT_EXT_TO_MIME[ext]:
        if ext == "json" and content.lstrip()[:1] in (b"{", b"["):
            return ext
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Type de fichier incoherent avec son extension.",
        )

    return ext


def _strip_pdf_annex(text: str) -> str:
    pattern = re.escape(_ANNEX_START) + r".*?" + re.escape(_ANNEX_END)
    return re.sub(pattern, "", text, flags=re.DOTALL).strip()


def _normalize_import_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _parse_role(label: str) -> str:
    return "user" if label in {"Utilisateur", "Vous"} else "assistant"


def _parse_exported_conversation_text(text: str, fallback_name: str) -> tuple[str, list[dict[str, str]]]:
    normalized = _normalize_import_text(text)
    lines = normalized.split("\n")
    title = fallback_name
    messages: list[dict[str, str]] = []
    current: dict[str, Any] | None = None

    def flush_current() -> None:
        nonlocal current
        if not current:
            return
        content = "\n".join(current["lines"]).strip()
        if content:
            messages.append({
                "role": current["role"],
                "timestamp": current["timestamp"],
                "content": content,
            })
        current = None

    ignored_prefixes = (
        "Exporte le:",
        "Exporte le ",
        "Privacy Proxy",
        "Conversation",
        "Date d'export",
        "Projet cree le",
        "Derniere activite",
        "Nombre d'echanges",
        "Statut",
        "AVERTISSEMENT RGPD",
        "Le bloc ci-dessous contient",
    )

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            if current:
                current["lines"].append("")
            continue

        if line.startswith("Projet:"):
            parsed_title = line.split(":", 1)[1].strip()
            if parsed_title:
                title = parsed_title
            continue

        if line.startswith(_ANNEX_START) or line.startswith(_ANNEX_END):
            continue

        if any(line.startswith(prefix) for prefix in ignored_prefixes):
            continue

        matched = None
        for pattern in _IMPORT_HEADER_PATTERNS:
            matched = pattern.match(line)
            if matched:
                break

        if matched:
            flush_current()
            current = {
                "role": _parse_role(matched.group(1)),
                "timestamp": matched.group(2).strip(),
                "lines": [],
            }
            continue

        if current:
            current["lines"].append(raw_line.rstrip())

    flush_current()

    if messages:
        return title, messages

    if normalized:
        return title, [{
            "role": "assistant",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "content": normalized,
        }]

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="Impossible d'extraire une conversation depuis ce fichier.",
    )


def _parse_imported_json(content: bytes, fallback_name: str) -> tuple[str, list[dict[str, str]]]:
    try:
        payload = json.loads(content.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="JSON d'import invalide ou corrompu.",
        ) from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Le JSON importe doit contenir une liste 'messages'.",
        )

    title = str(payload.get("project_name") or payload.get("project_title") or fallback_name)
    messages: list[dict[str, str]] = []
    for item in payload["messages"]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content_redacted = str(item.get("content_redacted") or "").strip()
        timestamp = str(item.get("timestamp") or datetime.now(timezone.utc).isoformat())
        if role not in {"user", "assistant"} or not content_redacted:
            continue
        messages.append({
            "role": role,
            "timestamp": timestamp,
            "content": content_redacted,
        })

    if not messages:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Le JSON importe ne contient aucun message exploitable.",
        )

    return title, messages


def _apply_mapping_to_text(text: str, mapping: dict[str, str]) -> str:
    redacted = text
    for placeholder, original in sorted(
        mapping.items(),
        key=lambda item: len(item[1] or ""),
        reverse=True,
    ):
        if original:
            redacted = redacted.replace(original, placeholder)
    return redacted


def _extract_entity_type(placeholder: str) -> str:
    match = _PLACEHOLDER_RE.match(placeholder)
    if not match:
        return "PII"
    return match.group(1)


async def _build_messages_from_json(messages: list[dict[str, str]]) -> tuple[list[ProjectMessage], dict[str, str]]:
    imported = [
        ProjectMessage(
            role=message["role"],
            content_redacted=message["content"],
            content_restored=message["content"],
            timestamp=message["timestamp"],
        )
        for message in messages
    ]
    return imported, {}


async def _build_messages_from_pdf(
    messages: list[dict[str, str]],
    mapping: dict[str, str],
) -> tuple[list[ProjectMessage], dict[str, str]]:
    imported = [
        ProjectMessage(
            role=message["role"],
            content_redacted=_apply_mapping_to_text(message["content"], mapping),
            content_restored=message["content"],
            timestamp=message["timestamp"],
        )
        for message in messages
    ]
    return imported, mapping


async def _build_messages_from_human_export(
    messages: list[dict[str, str]],
) -> tuple[list[ProjectMessage], dict[str, str]]:
    redactor = get_shared_redactor()
    imported: list[ProjectMessage] = []
    global_mapping: dict[str, str] = {}
    global_value_to_placeholder: dict[tuple[str, str], str] = {}
    counters: dict[str, int] = {}

    for message in messages:
        restored = message["content"]
        redacted, local_mapping = await redactor.smart_pseudonymize(restored)

        normalized_text = redacted
        for local_placeholder, original in local_mapping.items():
            entity_type = _extract_entity_type(local_placeholder)
            key = (entity_type, original)
            if key not in global_value_to_placeholder:
                counters[entity_type] = counters.get(entity_type, 0) + 1
                global_value_to_placeholder[key] = f"[{entity_type}_{counters[entity_type]}]"
            global_placeholder = global_value_to_placeholder[key]
            normalized_text = normalized_text.replace(local_placeholder, global_placeholder)
            global_mapping[global_placeholder] = original

        imported.append(ProjectMessage(
            role=message["role"],
            content_redacted=normalized_text,
            content_restored=restored,
            timestamp=message["timestamp"],
        ))

    return imported, global_mapping


async def _create_imported_project(
    *,
    current_user: TokenData,
    filename: str,
    title: str,
    messages: list[ProjectMessage],
    mapping: dict[str, str],
) -> Project:
    project = await _pm.create_project(current_user.username, first_message="")
    now = datetime.now(timezone.utc)
    project.name = title or _default_import_name(filename)
    project.created_at = now.isoformat()
    project.last_activity = messages[-1].timestamp if messages else now.isoformat()
    project.messages = messages
    project.messages_count = len([message for message in messages if message.role == "user"])
    project.session_expires_at = None

    if mapping:
        await _vault.store_mapping(project.session_id, mapping, user_id=current_user.username)
        project.session_expires_at = (now + timedelta(seconds=settings.REDIS_TTL_SECONDS)).isoformat()

    await _pm._save_project(project)
    return project


def _conversation_lines(project: Project, *, redacted: bool) -> list[str]:
    lines = [
        f"Projet: {project.name}",
        f"Exporte le: {datetime.now(timezone.utc).isoformat()}",
        "",
    ]
    for message in project.messages:
        label = "Utilisateur" if message.role == "user" else "Assistant"
        content = message.content_redacted if redacted else message.content_restored
        lines.extend([
            f"[{label}] {message.timestamp}",
            content or "",
            "",
        ])
    return lines


def _build_docx_bytes(project: Project) -> bytes:
    try:
        from docx import Document  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError("python-docx requis pour l'export DOCX.") from exc

    doc = Document()
    doc.add_heading(project.name, level=1)
    doc.add_paragraph(f"Exporte le {datetime.now(timezone.utc).isoformat()}")

    for message in project.messages:
        label = "Utilisateur" if message.role == "user" else "Assistant"
        doc.add_heading(f"{label} - {message.timestamp}", level=2)
        doc.add_paragraph(message.content_restored or "")

    output = BytesIO()
    doc.save(output)
    return output.getvalue()


def _build_txt_bytes(project: Project) -> bytes:
    text = "\n".join(_conversation_lines(project, redacted=False)).strip() + "\n"
    return text.encode("utf-8")


def _build_json_bytes(project: Project) -> bytes:
    payload = {
        "project_id_hash": _h(project.project_id),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "messages": [
            {
                "role": message.role,
                "content_redacted": message.content_redacted,
                "timestamp": message.timestamp,
            }
            for message in project.messages
        ],
        "vault_key_hint": f"session_expires_at:{project.session_expires_at or 'unknown'}",
        "warning": (
            "Ce fichier contient des donnees pseudonymisees. "
            "Le mapping de re-identification a expire."
        ),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


async def _export_binary_response(
    *,
    current_user: TokenData,
    project_id: str,
    file_type: str,
    builder: Callable[[Project], bytes],
    media_type: str,
) -> Response:
    _validate_project_id(project_id)
    org_id, require_audit, project = await _load_export_context(project_id, current_user)

    try:
        if file_type == "pdf":
            vault_mapping = await _vault.get_mapping(project.session_id, current_user.username) or {}
            payload_bytes = export_conversation(project, vault_mapping)
        else:
            payload_bytes = builder(project)
    except Exception as exc:
        logger.error("export.%s ERREUR | project=%s | %s", file_type, project_id, type(exc).__name__)
        await _log_export_event(
            current_user=current_user,
            org_id=org_id,
            project_id=project_id,
            require_audit=require_audit,
            file_type=file_type,
            success=False,
            error_code=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Erreur lors de la generation du fichier {file_type}.",
        ) from exc

    logger.info(
        "export.%s.done | project=%s | user=%s | size=%d",
        file_type,
        _h(project_id),
        _h(current_user.username),
        len(payload_bytes),
    )
    await _log_export_event(
        current_user=current_user,
        org_id=org_id,
        project_id=project_id,
        require_audit=require_audit,
        file_type=file_type,
        success=True,
    )

    extension = "json" if file_type == "json" else file_type
    return Response(
        content=payload_bytes,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{_safe_filename(project, extension)}"',
            "X-RGPD-Warning": (
                "Ce fichier contient des donnees pseudonymisees."
                if file_type == "json"
                else "Ce fichier contient des donnees personnelles reelles."
            ),
        },
    )


@router.get(
    "/projects/{project_id}/export",
    summary="Exporte la conversation en PDF (donnees re-identifiees)",
    responses={200: {"content": {"application/pdf": {}}}},
)
@limiter.limit("10/minute")
async def export_project_pdf(
    request: Request,
    project_id: str,
    current_user: TokenData = Depends(get_current_user),
) -> Response:
    return await _export_binary_response(
        current_user=current_user,
        project_id=project_id,
        file_type="pdf",
        builder=lambda project: b"",
        media_type="application/pdf",
    )


@router.get(
    "/projects/{project_id}/export/docx",
    summary="Exporte la conversation en DOCX",
    responses={200: {"content": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document": {}}}},
)
@limiter.limit("10/minute")
async def export_project_docx(
    request: Request,
    project_id: str,
    current_user: TokenData = Depends(get_current_user),
) -> Response:
    return await _export_binary_response(
        current_user=current_user,
        project_id=project_id,
        file_type="docx",
        builder=_build_docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@router.get(
    "/projects/{project_id}/export/txt",
    summary="Exporte la conversation en TXT",
    responses={200: {"content": {"text/plain": {}}}},
)
@limiter.limit("10/minute")
async def export_project_txt(
    request: Request,
    project_id: str,
    current_user: TokenData = Depends(get_current_user),
) -> Response:
    return await _export_binary_response(
        current_user=current_user,
        project_id=project_id,
        file_type="txt",
        builder=_build_txt_bytes,
        media_type="text/plain; charset=utf-8",
    )


@router.get(
    "/projects/{project_id}/export/json",
    summary="Exporte la conversation pseudonymisee en JSON",
    responses={200: {"content": {"application/json": {}}}},
)
@limiter.limit("10/minute")
async def export_project_json(
    request: Request,
    project_id: str,
    current_user: TokenData = Depends(get_current_user),
) -> Response:
    return await _export_binary_response(
        current_user=current_user,
        project_id=project_id,
        file_type="json",
        builder=_build_json_bytes,
        media_type="application/json; charset=utf-8",
    )


@router.post(
    "/projects/import",
    summary="Reimporte une conversation depuis un export PDF, DOCX, TXT ou JSON",
)
@limiter.limit("10/minute")
async def import_project(
    request: Request,
    file: UploadFile = File(...),
    current_user: TokenData = Depends(get_current_user),
) -> dict:
    content = await file.read()
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Fichier d'import trop volumineux (max 20 Mo).",
        )

    ext = _detect_import_extension(file.filename or "", content)
    fallback_name = _default_import_name(file.filename or "")

    if ext == "json":
        title, raw_messages = _parse_imported_json(content, fallback_name)
        project_messages, mapping = await _build_messages_from_json(raw_messages)
    else:
        extracted_text = await extract_text(content, file.filename or f"import.{ext}")
        if ext == "pdf":
            extracted_text = _strip_pdf_annex(extracted_text)
        title, raw_messages = _parse_exported_conversation_text(extracted_text, fallback_name)

        if ext == "pdf":
            vault_b64 = extract_vault_from_pdf_bytes(content)
            if vault_b64:
                try:
                    restored_mapping = decrypt_mapping_b64(vault_b64)
                except Exception as exc:
                    logger.error(
                        "import.pdf.decrypt ERREUR | user=%s | %s",
                        _h(current_user.username),
                        type(exc).__name__,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="Impossible de dechiffrer le mapping du PDF importe.",
                    ) from exc
                project_messages, mapping = await _build_messages_from_pdf(raw_messages, restored_mapping)
            else:
                project_messages, mapping = await _build_messages_from_human_export(raw_messages)
        else:
            project_messages, mapping = await _build_messages_from_human_export(raw_messages)

    new_project = await _create_imported_project(
        current_user=current_user,
        filename=file.filename or "",
        title=title,
        messages=project_messages,
        mapping=mapping,
    )
    logger.info(
        "import.project.done | user=%s | new_project=%s | ext=%s | messages=%d | tokens=%d",
        _h(current_user.username),
        _h(new_project.project_id),
        ext,
        len(project_messages),
        len(mapping),
    )

    return {
        "project_id": new_project.project_id,
        "session_id": new_project.session_id,
        "imported_format": ext,
        "messages_imported": len(project_messages),
        "tokens_restored": len(mapping),
        "message": (
            f"Projet importe depuis {ext.upper()} avec {len(project_messages)} message(s). "
            f"Contexte de pseudonymisation restaure: {len(mapping)} token(s)."
        ),
    }
