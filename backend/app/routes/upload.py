"""
Route d'upload de fichiers.

POST /upload : accepte PDF, DOCX et TXT, pipeline complet via process_file() :
               extraction in-memory -> chunking -> pseudonymisation par chunk
               -> stockage vault -> reponse JSON avec metadonnees.

Authentification : JWT Bearer via Depends(get_current_user)
Rate limit       : 10 req/min (extraction + pseudonymisation couteuses en CPU)

Securite fichier :
  - Extension validee contre settings.ALLOWED_FILE_TYPES (source de verite config)
  - Magic bytes valides via python-magic (defense contre extension spoofing)

Logs : type + taille du fichier -- aucun contenu sensible (zero-content RGPD).
"""

import asyncio
import base64
import io
import json
import mimetypes
import traceback
from datetime import datetime, timezone
from functools import partial
from typing import Optional

import magic as _magic
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.api.routes.auth import TokenData, get_current_user
from app.config import settings
from app.core.audit_trail import AuditEvent, get_audit_trail
from app.core.business_detector import BusinessDetector
from app.core.file_processor import (
    process_file,
    extract_text as extract_text_fn,
    process_excel,
    export_excel_reidentified,
    inspect_excel_controls,
)
from app.core.policy_engine import ProtectionMode, get_effective_policy
from app.core.usage_tracker import estimate_local_processing_cost, track_usage
from app.core.redactor import get_shared_redactor
from app.core.vault import Vault, decrypt_and_migrate, encrypt_data
from app.middleware.rate_limit import limiter
from app.utils.logger import get_logger, hash_id as _h

logger = get_logger(__name__)
router = APIRouter()

# Singleton vault
_vault = Vault()

# MIME types acceptes (mapped aux extensions settings.ALLOWED_FILE_TYPES)
_EXT_TO_MIME: dict[str, set[str]] = {
    "pdf":  {"application/pdf"},
    "docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
    },
    "doc": {
        "application/msword",
        "application/x-tika-msoffice",
    },
    "odt": {
        "application/vnd.oasis.opendocument.text",
        "application/zip",
    },
    "xlsx": {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/zip",
    },
    "xls": {
        "application/vnd.ms-excel",
        "application/x-tika-msoffice",
    },
    "ods": {
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/zip",
    },
    "pptx": {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/zip",
    },
    "ppt": {
        "application/vnd.ms-powerpoint",
        "application/x-tika-msoffice",
    },
    "odp": {
        "application/vnd.oasis.opendocument.presentation",
        "application/zip",
    },
    "txt":  {"text/plain"},
    "csv":  {"text/plain", "text/csv", "application/csv", "application/vnd.ms-excel"},
    "tsv":  {"text/tab-separated-values", "text/plain", "application/octet-stream"},
    "rtf":  {"application/rtf", "text/rtf", "text/plain"},
    "md":   {"text/markdown", "text/plain"},
    "jpg":  {"image/jpeg"},
    "jpeg": {"image/jpeg"},
    "png":  {"image/png"},
    "gif":  {"image/gif"},
    "webp": {"image/webp"},
    "bmp":  {"image/bmp", "image/x-ms-bmp"},
    "tiff": {"image/tiff"},
    "tif":  {"image/tiff"},
    "eml":  {"message/rfc822", "text/plain"},
    "msg": {
        "application/vnd.ms-outlook",
        "application/x-ole-storage",
        "application/octet-stream",
    },
    "vtt":  {"text/vtt", "text/plain"},
    "srt":  {"text/plain", "application/x-subrip"},
    "html": {"text/html", "text/plain"},
    "htm":  {"text/html", "text/plain"},
    "json": {"application/json", "text/plain"},
    "xml":  {"application/xml", "text/xml", "text/plain"},
    "yaml": {"application/yaml", "application/x-yaml", "text/yaml", "text/plain"},
    "yml": {"application/yaml", "application/x-yaml", "text/yaml", "text/plain"},
}

# Ensemble de tous les MIME acceptes (union de _EXT_TO_MIME)
_ALL_ALLOWED_MIMES: frozenset[str] = frozenset(
    mime for mimes in _EXT_TO_MIME.values() for mime in mimes
)
from app.core.job_queue import get_job_queue
_OCR_IMAGE_EXTENSIONS: frozenset[str] = frozenset({
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "tif",
})
_DENIED_MIMES: frozenset[str] = frozenset({
    "application/x-dosexec",
    "application/x-msdownload",
    "application/x-executable",
    "application/x-sh",
    "text/x-python",
    "application/x-mach-binary",
    "application/x-elf",
})
_ASYNC_UPLOAD_EXTENSIONS: frozenset[str] = frozenset({
    "xlsx", "xls", "ods",
    "pptx", "ppt", "odp",
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "tif",
    "pdf",
})
_CLIENT_REDACTED_TEXT_MAX_CHARS = 12000


def _excel_workbook_key(user_id: str, session_id: str) -> str:
    return f"excel_wb:{user_id}:{session_id}"


async def _scan_keys(redis, pattern: str, count: int = 100) -> list[str]:
    keys: list[str] = []
    cursor = 0
    while True:
        cursor, batch = await redis.scan(cursor, match=pattern, count=count)
        keys.extend(batch)
        if cursor == 0:
            break
    return keys


def _require_org_membership(current_user: TokenData) -> str:
    org_id = current_user.org_id or ""
    if not org_id:
        if current_user.role == "admin":
            return ""
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Compte non rattache a une organisation.",
        )
    return org_id


def _allowed_extensions() -> frozenset[str]:
    """Retourne les extensions autorisees depuis la config (source de verite)."""
    return frozenset(settings.ALLOWED_FILE_TYPES)


def _should_enqueue_upload(ext: str, size_mb: float) -> bool:
    if size_mb > 1.0:
        return True
    return ext in _ASYNC_UPLOAD_EXTENSIONS


def _prepare_client_redacted_text(redacted_text: str) -> str:
    text = str(redacted_text or "")
    if len(text) <= _CLIENT_REDACTED_TEXT_MAX_CHARS:
        return text
    return (
        f"{text[:_CLIENT_REDACTED_TEXT_MAX_CHARS]}\n\n"
        "[Document tronque dans la reponse upload pour accelerer le transfert UI]"
    )


def _build_project_attachment_excerpt(redacted_text: str) -> str:
    text = str(redacted_text or "").strip()
    if not text:
        return ""
    max_chars = 16000
    if len(text) <= max_chars:
        return text
    return (
        f"{text[:max_chars]}\n\n"
        "[Document tronque dans l'historique du projet pour limiter les tokens]"
    )


async def get_effective_mode(
    user_id: str,
    org_id: str,
    requested_mode: str,
    project_name: Optional[str] = None,
    org_settings: Optional[object] = None,
) -> str:
    current_settings = org_settings
    if org_id and current_settings is None:
        from app.core.org_manager import OrgNotFoundError, get_org_manager  # noqa: PLC0415

        try:
            current_settings = await get_org_manager().get_settings(org_id)
        except OrgNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Compte non rattache a une organisation.",
            ) from exc

    strict_departments = getattr(
        getattr(current_settings, "policy", None),
        "departments_strict_mode",
        [],
    ) or []
    if project_name:
        project_name_lower = project_name.lower()
        for department in strict_departments:
            if department and department.lower() in project_name_lower:
                if requested_mode != "strict":
                    logger.info(
                        "upload.mode_forced_strict | user=%s | org=%s | department=%s",
                        _h(user_id),
                        _h(org_id),
                        department.lower(),
                    )
                return "strict"

    minimum_mode = getattr(current_settings, "min_mode", None) if current_settings else None
    return get_effective_policy(requested_mode, minimum_mode).mode.value


async def _safe_audit_log(event: AuditEvent) -> None:
    try:
        await get_audit_trail().log(event)
    except Exception as exc:
        logger.warning("upload.audit WARN | %s", type(exc).__name__)


def _validate_magic(content: bytes, ext: str) -> bool:
    """
    Verifie que les magic bytes du fichier correspondent a l'extension declaree.
    Defense contre le renommage frauduleux (ex: exe renomme en pdf).

    Retourne True si le MIME detecte est coherent avec l'extension.
    En cas d'erreur libmagic, leve une HTTPException 422.
    """
    try:
        detected_mime: str = _magic.from_buffer(content[:2048], mime=True)
    except Exception as exc:
        logger.warning("upload.magic_check WARN | fallback_extension_only | %s", type(exc).__name__)
        detected_mime = mimetypes.guess_type(f"file.{ext}")[0] or "application/octet-stream"

    if detected_mime in _DENIED_MIMES:
        return False

    # Le MIME doit etre dans l'ensemble global accepte ET coherent avec l'extension
    if detected_mime not in _ALL_ALLOWED_MIMES:
        return False
    ext_mimes = _EXT_TO_MIME.get(ext, set())
    return detected_mime in ext_mimes


# ---------------------------------------------------------------------------
# Schema de reponse
# ---------------------------------------------------------------------------

class UploadResponse(BaseModel):
    session_id: str
    project_id: Optional[str]
    filename: str
    file_type: str
    original_size_chars: int
    chunks_count: int
    total_entities: int
    redacted_entities: int
    token_estimate: int
    redacted_text: str
    ocr_available: bool = False
    ocr_used: bool = False
    is_excel: bool = False
    excel_report: Optional[dict] = None
    excel_controls: Optional[dict] = None


class UploadJobQueuedResponse(BaseModel):
    status: str
    job_id: str
    message: str


def _parse_json_list(raw_value: Optional[str], field_name: str) -> list[str]:
    if raw_value is None or not raw_value.strip():
        return []

    try:
        data = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_name} doit etre une liste JSON de chaines.",
        ) from exc

    if not isinstance(data, list) or any(not isinstance(item, str) for item in data):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_name} doit etre une liste JSON de chaines.",
        )
    return data


async def _load_org_settings_for_upload(org_id: str):
    if not org_id:
        return None

    from app.core.org_manager import OrgNotFoundError, get_org_manager  # noqa: PLC0415

    try:
        return await get_org_manager().get_settings(org_id)
    except OrgNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Compte non rattache a une organisation.",
        ) from exc


async def _process_upload_content(
    *,
    filename: str,
    content: bytes,
    user_id: str,
    org_id: str,
    project_id: Optional[str],
    session_id_override: Optional[str],
    protection_mode: str,
    excluded_sheets_list: list[str],
    excluded_columns_list: list[str],
    excluded_ranges_list: list[str],
) -> tuple[dict, str, int, float, Optional[str]]:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    estimated_processing_cost = estimate_local_processing_cost(len(content))
    org_settings_up = await _load_org_settings_for_upload(org_id) if org_id else None
    org_policy = getattr(org_settings_up, "policy", None)

    from app.core.project_manager import ProjectManager  # noqa: PLC0415

    _pm = ProjectManager()
    project = None
    resolved_project_id: Optional[str] = project_id
    if project_id:
        project = await _pm.get_project(project_id, user_id)
        if project is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Projet {project_id!r} introuvable ou acces refuse.",
            )
        session_id = project.session_id
        resolved_project_id = project.project_id
        await _pm.register_project_session(project.project_id, session_id)
    else:
        session_id = session_id_override or _vault.new_session()

    effective_mode = await get_effective_mode(
        user_id,
        org_id,
        protection_mode,
        project_name=project.name if project else None,
        org_settings=org_settings_up,
    )
    policy = get_effective_policy(
        effective_mode,
        getattr(org_policy, "min_protection_mode", "privacy"),
    )
    effective_excluded_sheets = list(dict.fromkeys(excluded_sheets_list))
    effective_excluded_columns = list(dict.fromkeys(excluded_columns_list))

    is_excel_file = ext in {"xlsx", "xls"}
    process_result: dict
    if is_excel_file:
        process_result = await process_excel(
            file_bytes=content,
            filename=filename,
            user_id=user_id,
            session_id=session_id,
            excluded_sheets=effective_excluded_sheets,
            excluded_columns=effective_excluded_columns,
            excluded_ranges=excluded_ranges_list,
            vault=_vault,
        )
        chunks_count = 1
        total_entities = process_result["entities_count"]
        token_estimate = process_result["token_estimate"]
        redacted_text = process_result["markdown"]
        original_chars = len(content)
        try:
            _redis_ex = await _pm._get_redis()
            if _redis_ex:
                _ttl_ex = getattr(settings, "REDIS_TTL_SECONDS", 3600)
                encrypted_workbook = encrypt_data(
                    process_result["workbook_bytes"],
                    settings.VAULT_ENCRYPTION_KEY,
                    org_id,
                )
                await _redis_ex.setex(
                    _excel_workbook_key(user_id, session_id),
                    _ttl_ex,
                    encrypted_workbook,
                )
        except Exception as _exc_wb:
            logger.warning("upload.excel_wb_store WARN | %s", type(_exc_wb).__name__)
    else:
        process_result = await process_file(
            file_bytes=content,
            filename=filename,
            redactor=get_shared_redactor(),
            vault=_vault,
            session_id=session_id,
            user_id=user_id,
            entities=list(policy.active_entities),
            min_confidence_score=policy.min_confidence_score,
            excluded_entity_types=list(policy.excluded_entity_types),
        )
        chunks_count = process_result["chunks_count"]
        total_entities = process_result["total_entities"]
        token_estimate = process_result["token_estimate"]
        redacted_text = process_result["redacted_text"]
        original_chars = process_result["original_chars"]

    if resolved_project_id is None:
        project = await _pm.create_project(
            user_id=user_id,
            first_message="",
            session_id=session_id,
        )
        resolved_project_id = project.project_id
        logger.info(
            "upload.project_create | project=%s | session=%s | user=%s",
            _h(project.project_id), _h(session_id), _h(user_id),
        )
    else:
        logger.info(
            "upload.project_attach | project=%s | session=%s | user=%s",
            _h(resolved_project_id), _h(session_id), _h(user_id),
        )

    if project is None and resolved_project_id:
        project = await _pm.get_project(resolved_project_id, user_id)
    if project is not None:
        await _pm.add_attachment_context(
            project,
            filename,
            _build_project_attachment_excerpt(redacted_text),
        )

    logger.info(
        "upload.done | session=%s | user=%s | chunks=%d | entities=%d",
        _h(session_id), _h(user_id), chunks_count, total_entities,
    )

    asyncio.ensure_future(track_usage(
        user_id=user_id,
        org_id=org_id,
        model="local-processing",
        provider="local",
        input_tokens=token_estimate,
        output_tokens=0,
        cost_usd=estimated_processing_cost,
        request_type="upload",
        session_id=session_id,
    ))

    response = UploadResponse(
        session_id=session_id,
        project_id=resolved_project_id,
        filename=filename,
        file_type=ext,
        original_size_chars=original_chars,
        chunks_count=chunks_count,
        total_entities=total_entities,
        redacted_entities=total_entities,
        token_estimate=token_estimate,
        redacted_text=_prepare_client_redacted_text(redacted_text),
        ocr_available=settings.OCR_ENABLED,
        ocr_used=ext in _OCR_IMAGE_EXTENSIONS and settings.OCR_ENABLED,
        is_excel=is_excel_file,
        excel_report=process_result.get("report") if is_excel_file else None,
        excel_controls=process_result.get("excel_controls") if is_excel_file else None,
    )
    return (
        response.model_dump(),
        effective_mode,
        total_entities,
        estimated_processing_cost,
        resolved_project_id,
    )


async def run_upload_job_worker() -> None:
    job_queue = await get_job_queue()
    logger.info("upload.worker_started")
    try:
        while True:
            job = await job_queue.dequeue(timeout=5)
            if not job:
                await asyncio.sleep(0)
                continue

            job_id = str(job.get("job_id") or "")
            job_type = str(job.get("type") or "")
            payload = job.get("payload") or {}
            user_id = str(payload.get("user_id") or "")
            org_id = str(payload.get("org_id") or "")
            filename = str(payload.get("filename") or "")
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else None

            try:
                logger.info(
                    "upload.worker_job_start | job=%s | type=%s | user=%s | file=%s",
                    _h(job_id),
                    job_type,
                    _h(user_id),
                    _h(filename),
                )
                await job_queue.set_status(
                    job_id,
                    "processing",
                    user_id=user_id,
                    started_at=datetime.now(timezone.utc).isoformat(),
                )

                if job_type != "process_file":
                    raise ValueError(f"Type de job non supporte: {job_type}")

                file_b64 = payload.get("file_b64")
                if not isinstance(file_b64, str) or not file_b64:
                    raise ValueError("Payload de job incomplet.")

                content = base64.b64decode(file_b64.encode("utf-8"))
                (
                    result,
                    effective_mode,
                    total_entities,
                    estimated_processing_cost,
                    resolved_project_id,
                ) = await _process_upload_content(
                    filename=filename,
                    content=content,
                    user_id=user_id,
                    org_id=org_id,
                    project_id=payload.get("project_id"),
                    session_id_override=payload.get("session_id"),
                    protection_mode=str(payload.get("protection_mode") or ProtectionMode.PRIVACY.value),
                    excluded_sheets_list=list(payload.get("excluded_sheets") or []),
                    excluded_columns_list=list(payload.get("excluded_columns") or []),
                    excluded_ranges_list=list(payload.get("excluded_ranges") or []),
                )

                await job_queue.set_result(
                    job_id,
                    {
                        "job_id": job_id,
                        "status": "done",
                        "user_id": user_id,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "result": result,
                    },
                )
                logger.info(
                    "upload.worker_job_done | job=%s | user=%s | file=%s",
                    _h(job_id),
                    _h(user_id),
                    _h(filename),
                )
                await _safe_audit_log(
                    AuditEvent.build(
                        user_id=user_id,
                        org_id=org_id,
                        project_id=resolved_project_id,
                        action="upload",
                        model_used="local-processing",
                        protection_mode=effective_mode,
                        entities_masked=total_entities,
                        file_type=ext,
                        cost_usd=estimated_processing_cost,
                        success=True,
                    )
                )
            except HTTPException as exc:
                await job_queue.set_result(
                    job_id,
                    {
                        "job_id": job_id,
                        "status": "error",
                        "user_id": user_id,
                        "error": exc.detail,
                        "error_code": str(exc.status_code),
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                await _safe_audit_log(
                    AuditEvent.build(
                        user_id=user_id,
                        org_id=org_id,
                        project_id=payload.get("project_id"),
                        action="upload",
                        model_used="local-processing",
                        protection_mode=str(payload.get("protection_mode") or ""),
                        entities_masked=0,
                        file_type=ext,
                        cost_usd=0.0,
                        success=False,
                        error_code=str(exc.status_code),
                    )
                )
            except Exception as exc:
                logger.error(
                    "upload.worker_failed | job=%s | user=%s | error=%s | trace=%s",
                    _h(job_id),
                    _h(user_id) if user_id else "?",
                    str(exc),
                    traceback.format_exc(),
                )
                await job_queue.set_result(
                    job_id,
                    {
                        "job_id": job_id,
                        "status": "error",
                        "user_id": user_id,
                        "error": "Le traitement du fichier a échoué.",
                        "error_code": type(exc).__name__,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                await _safe_audit_log(
                    AuditEvent.build(
                        user_id=user_id,
                        org_id=org_id,
                        project_id=payload.get("project_id"),
                        action="upload",
                        model_used="local-processing",
                        protection_mode=str(payload.get("protection_mode") or ""),
                        entities_masked=0,
                        file_type=ext,
                        cost_usd=0.0,
                        success=False,
                        error_code=type(exc).__name__,
                    )
                )
    except asyncio.CancelledError:
        logger.info("upload.worker_stopped")
        raise


# ---------------------------------------------------------------------------
# POST /upload
# ---------------------------------------------------------------------------

@router.post(
    "/upload",
    response_model=None,
    status_code=status.HTTP_200_OK,
    summary="Upload fichiers structurés -- extraction + chunking + pseudonymisation PII",
    description=(
        "Extrait le texte brut d'un fichier en memoire, decoupe en chunks avec overlap, "
        "pseudonymise les PII de chaque chunk et stocke le mapping dans le vault. "
        f"Taille maximale : {settings.MAX_FILE_SIZE_MB} Mo. "
        "Types acceptes definis dans settings.ALLOWED_FILE_TYPES."
    ),
)
@limiter.limit("10/minute")
async def upload_file(
    request: Request,
    file: UploadFile = File(..., description="Fichier a pseudonymiser"),
    project_id: Optional[str] = Form(None),
    protection_mode: str = Form(ProtectionMode.PRIVACY.value),
    excluded_sheets: Optional[str] = Form(None),
    excluded_columns: Optional[str] = Form(None),
    excluded_ranges: Optional[str] = Form(None),
    current_user: TokenData = Depends(get_current_user),
):
    filename = file.filename or ""
    allowed_exts = _allowed_extensions()
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    org_id = _require_org_membership(current_user)
    org_settings_up = None
    if org_id:
        from app.core.org_manager import OrgNotFoundError, get_org_manager  # noqa: PLC0415

        try:
            org_settings_up = await get_org_manager().get_settings(org_id)
        except OrgNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Compte non rattache a une organisation.",
            ) from exc

    effective_mode = protection_mode
    total_entities = 0
    resolved_project_id: Optional[str] = project_id
    estimated_processing_cost = 0.0

    try:
        if ext not in allowed_exts:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Type de fichier non supporte : {ext!r}. "
                    f"Types acceptes : {sorted(allowed_exts)}."
                ),
            )

        content = await file.read()
        if not content:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Le fichier est vide.",
            )

        if not _validate_magic(content, ext):
            logger.warning(
                "upload.magic_mismatch | user=%s | ext=%s",
                _h(current_user.username), ext,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Le contenu du fichier ne correspond pas a l'extension .{ext}. "
                    "Verifiez que le fichier n'est pas corrompu ou renomme."
                ),
            )

        size_mb = len(content) / (1024 * 1024)
        estimated_processing_cost = estimate_local_processing_cost(len(content))
        logger.info(
            "upload.start | user=%s | type=%s | size=%.2fMo",
            _h(current_user.username), ext, size_mb,
        )

        if org_settings_up and org_settings_up.max_tokens_per_user > 0:
            from app.core.usage_tracker import get_monthly_usage  # noqa: PLC0415

            _usage_up = await get_monthly_usage(current_user.username)
            if _usage_up.total_tokens >= org_settings_up.max_tokens_per_user:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Quota mensuel atteint ({org_settings_up.max_tokens_per_user} tokens).",
                )

        org_policy = getattr(org_settings_up, "policy", None)
        if org_policy:
            if org_policy.allowed_file_types and ext not in set(org_policy.allowed_file_types):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Le type de fichier {ext!r} est interdit par la politique de l'organisation.",
                )
            if org_policy.max_file_size_mb > 0 and size_mb > org_policy.max_file_size_mb:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"Fichier trop volumineux pour l'organisation (max {org_policy.max_file_size_mb} Mo).",
                )

        parsed_excluded_sheets = _parse_json_list(excluded_sheets, "excluded_sheets")
        parsed_excluded_columns = _parse_json_list(excluded_columns, "excluded_columns")
        parsed_excluded_ranges = _parse_json_list(excluded_ranges, "excluded_ranges")
        session_id = _vault.new_session()
        if project_id:
            from app.core.project_manager import ProjectManager  # noqa: PLC0415

            _pm = ProjectManager()
            project = await _pm.get_project(project_id, current_user.username)
            if project is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Projet {project_id!r} introuvable ou acces refuse.",
                )
            session_id = project.session_id
            resolved_project_id = project.project_id

        if _should_enqueue_upload(ext, size_mb):
            job_queue = await get_job_queue()
            job_id = await job_queue.enqueue(
                "process_file",
                {
                    "filename": filename,
                    "file_b64": base64.b64encode(content).decode("utf-8"),
                    "user_id": current_user.username,
                    "org_id": org_id,
                    "project_id": resolved_project_id,
                    "session_id": session_id,
                    "protection_mode": protection_mode,
                    "excluded_sheets": parsed_excluded_sheets,
                    "excluded_columns": parsed_excluded_columns,
                    "excluded_ranges": parsed_excluded_ranges,
                },
            )
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content=UploadJobQueuedResponse(
                    status="processing",
                    job_id=job_id,
                    message="Fichier en cours de traitement. Verifiez le statut dans quelques secondes.",
                ).model_dump(),
            )

        (
            response,
            effective_mode,
            total_entities,
            estimated_processing_cost,
            resolved_project_id,
        ) = await _process_upload_content(
            filename=filename,
            content=content,
            user_id=current_user.username,
            org_id=org_id,
            project_id=resolved_project_id,
            session_id_override=session_id,
            protection_mode=protection_mode,
            excluded_sheets_list=parsed_excluded_sheets,
            excluded_columns_list=parsed_excluded_columns,
            excluded_ranges_list=parsed_excluded_ranges,
        )

        await _safe_audit_log(
            AuditEvent.build(
                user_id=current_user.username,
                org_id=org_id,
                project_id=resolved_project_id,
                action="upload",
                model_used="local-processing",
                protection_mode=effective_mode,
                entities_masked=total_entities,
                file_type=ext,
                cost_usd=estimated_processing_cost,
                success=True,
            )
        )
        return response
    except HTTPException as exc:
        await _safe_audit_log(
            AuditEvent.build(
                user_id=current_user.username,
                org_id=org_id,
                project_id=resolved_project_id,
                action="upload",
                model_used="local-processing",
                protection_mode=effective_mode,
                entities_masked=total_entities,
                file_type=ext or None,
                cost_usd=estimated_processing_cost,
                success=False,
                error_code=str(exc.status_code),
            )
        )
        raise
    except Exception as exc:
        await _safe_audit_log(
            AuditEvent.build(
                user_id=current_user.username,
                org_id=org_id,
                project_id=resolved_project_id,
                action="upload",
                model_used="local-processing",
                protection_mode=effective_mode,
                entities_masked=total_entities,
                file_type=ext or None,
                cost_usd=estimated_processing_cost,
                success=False,
                error_code=type(exc).__name__,
            )
        )
        raise


@router.get(
    "/upload/status/{job_id}",
    status_code=status.HTTP_200_OK,
    summary="Statut d'un job d'upload asynchrone",
)
@limiter.limit("60/minute")
async def upload_status(
    request: Request,
    job_id: str,
    current_user: TokenData = Depends(get_current_user),
):
    job_queue = await get_job_queue()
    result = await job_queue.get_result(job_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job introuvable ou expire.",
        )

    owner = result.get("user_id")
    if owner and owner != current_user.username:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acces interdit a ce job.",
        )
    return result


# ---------------------------------------------------------------------------
# POST /upload/excel-export
# ---------------------------------------------------------------------------

class ExcelExportRequest(BaseModel):
    session_id: str
    project_id: Optional[str] = None


@router.post(
    "/upload/excel-export",
    status_code=status.HTTP_200_OK,
    summary="Export Excel re-identifie",
    description=(
        "Recupere le workbook pseudonymise stocke en session et re-identifie "
        "toutes les cellules via le vault. Retourne un fichier .xlsx telecharge directement."
    ),
)
@limiter.limit("10/minute")
async def excel_export(
    request: Request,
    body: ExcelExportRequest,
    current_user: TokenData = Depends(get_current_user),
) -> StreamingResponse:
    org_id = _require_org_membership(current_user)
    require_audit = True
    if org_id:
        try:
            from app.core.org_manager import OrgNotFoundError, get_org_manager  # noqa: PLC0415

            org_settings = await get_org_manager().get_settings(org_id)
            require_audit = getattr(getattr(org_settings, "policy", None), "require_audit_for_export", True)
        except OrgNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Organisation introuvable ou desactivee. Contactez votre administrateur.",
            ) from exc

    # Recuperer le workbook pseudonymise depuis Redis
    workbook_bytes = None
    try:
        from app.core.project_manager import ProjectManager as _PMxp  # noqa: PLC0415
        _pm_xp = _PMxp()
        _redis_xp = await _pm_xp._get_redis()
        if _redis_xp:
            namespaced_key = _excel_workbook_key(current_user.username, body.session_id)
            encrypted_workbook = await _redis_xp.get(namespaced_key)
            if encrypted_workbook:
                workbook_bytes, migrated_workbook = decrypt_and_migrate(
                    encrypted_workbook,
                    settings.VAULT_ENCRYPTION_KEY,
                    org_id,
                )
                if migrated_workbook != encrypted_workbook:
                    await _redis_xp.setex(
                        namespaced_key,
                        getattr(settings, "REDIS_TTL_SECONDS", 3600),
                        migrated_workbook,
                    )
                    logger.info(
                        "vault.migrated_v1_to_v2 | user=%s | org=%s",
                        _h(current_user.username), _h(org_id),
                    )
            else:
                owner_probe = await _scan_keys(_redis_xp, f"excel_wb:*:{body.session_id}")
                if owner_probe:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Acces interdit a ce workbook temporaire.",
                    )
    except Exception as _exc_rd:
        if isinstance(_exc_rd, HTTPException):
            raise
        logger.warning("excel_export.redis WARN | %s", type(_exc_rd).__name__)

    if not workbook_bytes:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workbook introuvable ou expire. Veuillez re-uploader le fichier.",
        )

    try:
        result_bytes = await export_excel_reidentified(
            workbook_bytes=workbook_bytes,
            vault=_vault,
            user_id=current_user.username,
            session_id=body.session_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )

    logger.info(
        "excel_export.done | session=%s | user=%s | size=%d",
        _h(body.session_id), _h(current_user.username), len(result_bytes),
    )

    export_event = AuditEvent.build(
        user_id=current_user.username,
        org_id=org_id,
        project_id=body.project_id,
        action="export",
        model_used="local",
        protection_mode="",
        entities_masked=0,
        file_type="excel",
        cost_usd=0.0,
        success=True,
    )
    if require_audit:
        await get_audit_trail().log(export_event)
    else:
        await _safe_audit_log(export_event)

    safe_sid = body.session_id.replace("-", "")[:8]
    filename_out = "export_reidentified_%s.xlsx" % safe_sid
    disp = "attachment; filename=%s%s%s" % (chr(34), filename_out, chr(34))
    return StreamingResponse(
        io.BytesIO(result_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disp},
    )


# ---------------------------------------------------------------------------
# POST /upload/preview
# ---------------------------------------------------------------------------

class PreviewResponse(BaseModel):
    filename: str
    file_type: str
    total_entities: int
    breakdown: dict[str, int]
    preview: list[dict]
    explanation: list[dict]
    business_findings: list[dict]
    protection_mode: str
    strict_recommended: bool
    strict_warning: bool
    preview_chars: int
    ocr_available: bool = False
    ocr_used: bool = False
    excel_controls: Optional[dict] = None


@router.post(
    "/upload/preview",
    response_model=PreviewResponse,
    status_code=status.HTTP_200_OK,
    summary="Preview des entites PII detectees sans pseudonymisation complete",
    description=(
        "Extrait le texte du fichier et retourne le nombre et le type d'entites PII "
        "detectees, sans envoyer au LLM ni stocker dans le vault. "
        "Utiliser avant /upload pour afficher un resume a l'utilisateur."
    ),
)
@limiter.limit("20/minute")
async def upload_preview(
    request: Request,
    file: UploadFile = File(...),
    protection_mode: str = Form(ProtectionMode.PRIVACY.value),
    current_user: TokenData = Depends(get_current_user),
):
    filename = file.filename or ""
    allowed_exts = _allowed_extensions()
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    _org_id = _require_org_membership(current_user)
    org_settings = None
    if _org_id:
        from app.core.org_manager import OrgNotFoundError, get_org_manager

        try:
            org_settings = await get_org_manager().get_settings(_org_id)
        except OrgNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Compte non rattache a une organisation.",
            ) from exc

    if ext not in allowed_exts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Type de fichier non supporte : {ext!r}.",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Fichier vide.")

    org_policy = getattr(org_settings, "policy", None)
    if org_policy and org_policy.allowed_file_types and ext not in set(org_policy.allowed_file_types):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Le type de fichier {ext!r} est interdit par la politique de l'organisation.",
        )
    max_file_size_mb = getattr(org_policy, "max_file_size_mb", 0) if org_policy else 0
    if max_file_size_mb > 0 and (len(content) / (1024 * 1024)) > max_file_size_mb:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Fichier trop volumineux pour l'organisation (max {max_file_size_mb} Mo).",
        )

    if not _validate_magic(content, ext):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Le contenu du fichier ne correspond pas a l'extension .{ext}.",
        )

    try:
        text = await extract_text_fn(content, filename)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    preview_text = text[:8000]
    redactor = get_shared_redactor()
    business_detector = BusinessDetector()
    loop = asyncio.get_running_loop()
    effective_mode = await get_effective_mode(
        current_user.username,
        _org_id,
        protection_mode,
        project_name=None,
        org_settings=org_settings,
    )
    policy = get_effective_policy(effective_mode, getattr(org_settings, "min_mode", None) if org_settings else None)
    report = await loop.run_in_executor(
        None,
        partial(
            redactor.get_entity_report,
            preview_text,
            "fr",
            list(policy.active_entities),
            policy.min_confidence_score,
            list(policy.excluded_entity_types),
        ),
    )
    business_findings = []
    if policy.detect_business_sensitive:
        business_findings = await loop.run_in_executor(
            None,
            business_detector.detect,
            preview_text,
        )
    excel_controls = await inspect_excel_controls(content, filename) if ext in {"xlsx", "xls"} else None

    return {
        "filename": filename,
        "file_type": ext,
        "total_entities": report["total"],
        "breakdown": report["by_type"],
        "preview": report["preview"],
        "explanation": report["explanation"],
        "business_findings": business_findings,
        "protection_mode": policy.mode.value,
        "strict_recommended": bool(business_findings),
        "strict_warning": bool(business_findings) and policy.mode.value != ProtectionMode.STRICT.value,
        "preview_chars": len(text),
        "ocr_available": settings.OCR_ENABLED,
        "ocr_used": ext in _OCR_IMAGE_EXTENSIONS and settings.OCR_ENABLED,
        "excel_controls": excel_controls,
    }
