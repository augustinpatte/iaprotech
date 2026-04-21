"""
Privacy Proxy — Application FastAPI principale.

Flux de traitement :
  1. SecurityMiddleware valide le JWT, injecte SecurityContext dans request.state
  2. SlowAPIMiddleware applique le rate limiting (X-RateLimit-* headers)
  3. SecurityHeadersMiddleware injecte les headers de securite HTTP
  4. La route appelle le ChatService (pseudonymise -> LLM -> re-identifie)
  5. La reponse restauree est retournee au client

Routes enregistrees :
  /api/auth/*        -- login JWT, /me, register, bootstrap
  /api/chat          -- POST /chat (JSON), POST /chat/stream (SSE)
  /api/upload        -- POST /upload (PDF/DOCX)
  /api/session/*     -- GET + DELETE /session/{session_id}
  /api/projects/*    -- CRUD projets + export/import
  /api/organizations -- organisations multi-tenant
  /dashboard/*       -- usage et quota
  /api/proxy/*       -- routes legacy
"""

import asyncio
import traceback
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
import shutil

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.routes import auth, proxy
from app.api.exception_handlers import register_exception_handlers
from app.api.middleware.security import SecurityMiddleware
from app.routes import chat, upload, session, projects, export_routes, dashboard, organizations, admin, users, invitations
from app.config import settings, validate_config
from app.middleware.rate_limit import limiter
from app.utils.logger import get_logger, hash_id as _h

logger = get_logger(__name__)


async def check_dependencies() -> None:
    tesseract_path = shutil.which("tesseract")
    if not tesseract_path:
        logger.warning("Tesseract non installe | ocr_disabled=true")
        settings.OCR_ENABLED = False
        return

    try:
        import pytesseract  # noqa: PLC0415

        pytesseract.get_tesseract_version()
        logger.info("Tesseract disponible | path=%s", tesseract_path)
        settings.OCR_ENABLED = True
    except Exception as exc:
        logger.warning("Tesseract defaillant | %s | ocr_disabled=true", type(exc).__name__)
        settings.OCR_ENABLED = False


# ---------------------------------------------------------------------------
# Security headers middleware
# ---------------------------------------------------------------------------

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Injecte les headers de securite HTTP sur toutes les reponses.
    Conforme OWASP Secure Headers Project.
    """

    _HEADERS = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "X-XSS-Protection": "1; mode=block",
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Cache-Control": "no-store",
    }

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        for header, value in self._HEADERS.items():
            response.headers[header] = value
        return response


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


async def _ensure_dev_admin_credentials() -> None:
    if not settings.DEV_AUTO_CREATE_ADMIN:
        return

    username = (settings.DEV_ADMIN_USERNAME or "").strip()
    password = settings.DEV_ADMIN_PASSWORD or ""
    if not username or not password:
        logger.warning("startup.dev_admin_seed_skipped | reason=missing_credentials")
        return

    from app.api.routes.auth import _get_user, _set_user, pwd_context  # noqa: PLC0415
    from app.core.org_manager import get_org_manager  # noqa: PLC0415

    existing = await _get_user(username)
    now_iso = datetime.now(timezone.utc).isoformat()
    admin_org_id = ((existing or {}).get("org_id") or "").strip()
    if not admin_org_id:
        org = await get_org_manager().create_org(
            name="Organisation Principale",
            owner_id=username,
            plan="pro",
        )
        admin_org_id = org.org_id

    await _set_user(username, {
        "hashed_password": pwd_context.hash(password),
        "role": "admin",
        "status": "active",
        "plan": "max",
        "org_id": admin_org_id,
        "email": (existing or {}).get("email", ""),
        "invited_by": None,
        "invitations_used": int((existing or {}).get("invitations_used", 0) or 0),
        "created_at": (existing or {}).get("created_at", now_iso),
        "activated_at": (existing or {}).get("activated_at", now_iso),
        "last_login": (existing or {}).get("last_login"),
    })
    logger.warning(
        "startup.dev_admin_seeded | user=%s | local_testing_only=true",
        _h(username),
    )

    if admin_org_id:
        try:
            manager = get_org_manager()
            current_settings = await manager.get_settings(admin_org_id)
            policy = current_settings.policy.model_dump()
            policy["allowed_file_types"] = list(settings.ALLOWED_FILE_TYPES)
            await manager.update_settings(
                admin_org_id,
                {
                    "allowed_providers": ["anthropic", "openai", "google", "mistral"],
                    "policy": policy,
                },
            )
            logger.info(
                "startup.dev_admin_org_policy_synced | org=%s | file_types=%d",
                _h(admin_org_id),
                len(settings.ALLOWED_FILE_TYPES),
            )
        except Exception as exc:
            logger.warning(
                "startup.dev_admin_org_policy_sync_failed | user=%s | %s",
                _h(username),
                type(exc).__name__,
            )


async def _ensure_admin_org_membership() -> None:
    from app.api.routes.auth import _get_redis, _get_user, _set_user  # noqa: PLC0415
    from app.core.org_manager import get_org_manager  # noqa: PLC0415

    redis = await _get_redis()
    if redis is None:
        logger.warning("startup.admin_org_migration_skipped | reason=redis_unavailable")
        return

    cursor = 0
    manager = get_org_manager()
    while True:
        cursor, batch = await redis.scan(cursor, match="user:*", count=100)
        for key in batch:
            if key.count(":") != 1:
                continue

            username = key.split(":", 1)[1]
            user = await _get_user(username)
            if not user or user.get("role") != "admin":
                continue

            current_org_id = (user.get("org_id") or "").strip()
            if current_org_id:
                continue

            org = await manager.create_org(
                name="Organisation Principale",
                owner_id=username,
                plan="pro",
            )
            await _set_user(username, {
                **user,
                "role": "admin",
                "status": "active",
                "plan": user.get("plan") or "max",
                "org_id": org.org_id,
                "created_at": user.get("created_at") or datetime.now(timezone.utc).isoformat(),
                "activated_at": user.get("activated_at") or datetime.now(timezone.utc).isoformat(),
            })
            logger.warning(
                "startup.admin_org_migrated | user=%s | org=%s",
                _h(username),
                _h(org.org_id),
            )

        if cursor == 0:
            break


async def _prewarm_redactor() -> None:
    from app.core.redactor import get_shared_redactor_async  # noqa: PLC0415

    redactor = await get_shared_redactor_async()
    loop = asyncio.get_running_loop()
    warmups = [
        ("fr", "Jean Dupont 06 12 34 56 78 jean@example.com"),
        ("en", "John Smith +1 415 555 0101 john@example.com"),
    ]
    for language, sample in warmups:
        await loop.run_in_executor(
            None,
            redactor.get_entity_report,
            sample,
            language,
            None,
            0.6,
            None,
        )
    logger.info("startup.redactor_prewarmed | languages=%s", ",".join(lang for lang, _ in warmups))


@asynccontextmanager
async def lifespan(app: FastAPI):
    upload_worker_task = None
    try:
        validate_config()
        await check_dependencies()
        await _ensure_dev_admin_credentials()
        await _prewarm_redactor()
        # Desactive temporairement la migration org auto au demarrage pour
        # eviter qu'un compte de dev provoque un crash de worker.
        # await _ensure_admin_org_membership()
        upload_worker_task = asyncio.create_task(
            upload.run_upload_job_worker(),
            name="upload-job-worker",
        )
        from app.core.router import mask_secret  # noqa: PLC0415

        logger.info(
            "LLM secrets loaded | provider=%s | default_model=%s | anthropic=%s | openai=%s | gemini=%s | mistral=%s",
            settings.LLM_PROVIDER,
            settings.DEFAULT_MODEL,
            mask_secret(settings.ANTHROPIC_API_KEY),
            mask_secret(settings.OPENAI_API_KEY),
            mask_secret(settings.GEMINI_API_KEY),
            mask_secret(settings.MISTRAL_API_KEY),
        )

        logger.info(
            "Privacy Proxy demarre | debug=%s | provider=%s | engine=%s | redactor=prewarmed",
            settings.DEBUG, settings.LLM_PROVIDER, settings.REDACTION_ENGINE,
        )
    except Exception as exc:
        logger.critical(
            "DEMARRAGE_ECHOUE | L'application ne peut pas demarrer sainement | erreur=%s",
            exc,
            exc_info=True,
        )
        # Crash-fast : mieux vaut ne pas demarrer que demarrer casse.
        raise SystemExit(1) from exc

    yield

    if upload_worker_task is not None:
        upload_worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await upload_worker_task
    logger.info("Privacy Proxy arrete.")


app = FastAPI(
    title="Privacy Proxy",
    description=(
        "Proxy RGPD pour APIs LLM avec pseudonymisation automatique des PII. "
        "Toutes les donnees personnelles sont masquees avant d'etre transmises au LLM "
        "et restaurees dans la reponse."
    ),
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
)

# ---------------------------------------------------------------------------
# Exception handlers — enregistres avant les middlewares
# ---------------------------------------------------------------------------
@app.middleware("http")
async def log_exceptions(request: Request, call_next):
    try:
        response = await call_next(request)
        return response
    except Exception as exc:
        logger.error(
            "WORKER_CRASH | path=%s | error=%s | trace=%s",
            request.url.path,
            str(exc),
            traceback.format_exc(),
        )
        return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    max_size = settings.MAX_FILE_SIZE_MB * 1024 * 1024
    content_length = request.headers.get("content-length")

    # Cas 1 : Content-Length present → check rapide
    if content_length:
        try:
            if int(content_length) > max_size:
                return JSONResponse(
                    status_code=413,
                    content={
                        "detail": (
                            f"Fichier trop grand. Maximum : "
                            f"{settings.MAX_FILE_SIZE_MB}Mo"
                        )
                    },
                )
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"detail": "En-tete Content-Length invalide"},
            )
        return await call_next(request)

    # Cas 2 : pas de Content-Length (chunked) → streamer et limiter
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_size:
            return JSONResponse(
                status_code=413,
                content={
                    "detail": (
                        f"Fichier trop grand (chunked). Maximum : "
                        f"{settings.MAX_FILE_SIZE_MB}Mo"
                    )
                },
            )

    # Rebind le body pour que les handlers puissent le re-lire
    async def _receive():
        return {
            "type": "http.request",
            "body": bytes(body),
            "more_body": False,
        }

    request._receive = _receive
    return await call_next(request)


register_exception_handlers(app)

# ---------------------------------------------------------------------------
# Middleware stack (ordre d'execution : dernier enregistre = premier execute)
# ---------------------------------------------------------------------------

# 1. Rate limiter
# Limiter configure avec Redis (storage_uri=settings.REDIS_URL) pour partager
# les compteurs entre tous les workers uvicorn.
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

# 2. CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 3. Security headers (appliques sur toutes les reponses)
app.add_middleware(SecurityHeadersMiddleware)

# 4. JWT Auth + SecurityContext
app.add_middleware(SecurityMiddleware)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(auth.router,          prefix="/api/auth",          tags=["auth"])
app.include_router(chat.router,          prefix="/api",                tags=["chat"])
app.include_router(upload.router,        prefix="/api",                tags=["upload"])
app.include_router(session.router,       prefix="/api",                tags=["session"])
app.include_router(projects.router,      prefix="/api",                tags=["projects"])
app.include_router(users.router,         prefix="/api",                tags=["users"])
app.include_router(invitations.router,   prefix="/api",                tags=["invitations"])
app.include_router(export_routes.router, prefix="/api",                tags=["export"])
app.include_router(organizations.router, prefix="/api",                tags=["organizations"])
app.include_router(dashboard.router,     prefix="/dashboard",          tags=["dashboard"])
app.include_router(admin.router,         prefix="/api/admin",          tags=["admin"])
app.include_router(proxy.router,         prefix="/api/proxy",          tags=["proxy-legacy"])


# ---------------------------------------------------------------------------
# Endpoints systeme
# ---------------------------------------------------------------------------

@app.get("/health", tags=["system"])
async def health_check():
    """Liveness probe — teste Redis, circuit breaker et retourne ok/degraded."""
    redis_status = "unknown"
    circuit_state = "unknown"

    try:
        from app.core.vault import Vault  # noqa: PLC0415
        _v = Vault()
        info = await _v.health_check()
        redis_status = "connected" if info.get("redis_available") else "unavailable"
        circuit_state = info.get("circuit_state", "unknown")
    except Exception as exc:
        logger.warning("health_check.redis_probe_failed | %s", type(exc).__name__)
        redis_status = "error"

    return {
        "status": "ok" if redis_status == "connected" else "degraded",
        "redis": redis_status,
        "ocr": "available" if settings.OCR_ENABLED else "unavailable",
        "circuit_breaker": circuit_state,
        "version": "2.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data_retention": {
            "vault_ttl_hours": settings.VAULT_TTL_SECONDS // 3600,
            "project_restored_data": "purged_on_archive",
            "pseudonymized_history": "retained_for_project_lifetime",
        },
    }
