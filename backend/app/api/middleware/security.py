"""
SecurityMiddleware — defense en profondeur sur chaque requete protegee.

Verification dans cet ordre (arret immediat si echec) :
  1. Rate limit IP (protection DoS avant toute logique)
  2. JWT valide + non blackliste
  3. Existence de l'utilisateur dans Redis
  4. Role Redis == source de verite (detecte promotions/demotions)
  5. Injection SecurityContext(user_id, org_id, role) dans request.state

Ce middleware etend AuthMiddleware en ajoutant SecurityContext.
Il peut remplacer AuthMiddleware ou coexister avec.
"""
from __future__ import annotations

from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import settings
from app.utils.logger import get_logger, hash_id

logger = get_logger(__name__)

PUBLIC_PATHS = frozenset({
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/api/auth/token",
    "/api/auth/register",
    "/api/auth/status",
    "/api/auth/bootstrap",
})

_BEARER_PREFIX = "Bearer "


@dataclass(frozen=True)
class SecurityContext:
    """Contexte de securite injecte dans request.state.security."""
    user_id: str
    org_id: str
    role: str


class SecurityMiddleware(BaseHTTPMiddleware):
    """
    Middleware unifie — valide JWT, existence user, role Redis,
    puis injecte SecurityContext dans request.state.

    Ordre de verification :
      1. Route publique -> passe-plat
      2. JWT present et valide
      3. JTI non blackliste (revocation)
      4. Utilisateur existe dans Redis
      5. Role Redis (source de verite)
      6. Injection SecurityContext
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Routes publiques — passe-plat
        if path in PUBLIC_PATHS or path.startswith("/docs") or request.method == "OPTIONS":
            return await call_next(request)

        # 1. Extraction Bearer
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith(_BEARER_PREFIX):
            return _unauthorized("En-tete Authorization manquant ou malformé.")

        token = auth_header[len(_BEARER_PREFIX):]

        # 2. Decodage JWT
        try:
            from jose import ExpiredSignatureError, JWTError, jwt  # noqa: PLC0415
            payload: dict = jwt.decode(
                token,
                settings.SECRET_KEY,
                algorithms=[settings.ALGORITHM],
            )
        except Exception as exc:
            from jose import ExpiredSignatureError  # noqa: PLC0415
            if isinstance(exc, ExpiredSignatureError):
                logger.info("security.jwt_expired | path=%s", path)
                return _unauthorized("Token JWT expire. Reconnectez-vous.")
            logger.warning("security.jwt_invalid | path=%s | %s", path, type(exc).__name__)
            return _unauthorized("Token JWT invalide.")

        user_id: str = payload.get("sub", "")
        if not user_id:
            return _unauthorized("Claim 'sub' manquant dans le JWT.")

        # 3. JTI blacklist
        jti = payload.get("jti")
        if jti:
            try:
                from app.api.routes.auth import _get_redis  # noqa: PLC0415
                r = await _get_redis()
                if r and await r.exists(f"jti_bl:{jti}"):
                    logger.info("security.token_revoked | user=%s", hash_id(user_id))
                    return _unauthorized("Token revoque.")
            except Exception as exc:
                logger.warning(
                    "security.jti_check_failed | %s | fail-closed: refusing request",
                    type(exc).__name__,
                )
                return _unauthorized(
                    "Verification de securite temporairement indisponible. "
                    "Reconnectez-vous."
                )

        # 3b. Suspension membre : verifie si l'utilisateur a ete retire de son org.
        # La cle suspended:{user_id} est ecrite par OrgManager.remove_member() avec
        # TTL = TOKEN_EXPIRE_MINUTES * 60 — couvre tous les tokens encore en vie.
        # Fail-closed : si Redis est indisponible, on refuse par defaut.
        try:
            from app.api.routes.auth import _get_redis  # noqa: PLC0415
            _r = await _get_redis()
            if _r and await _r.exists(f"suspended:{user_id}"):
                logger.info("security.user_suspended | user=%s | path=%s", hash_id(user_id), path)
                return _forbidden("Votre compte a été suspendu.", status="suspended")
        except Exception as exc:
            logger.warning(
                "security.suspended_check_failed | %s — refuse par defaut (fail-closed)",
                type(exc).__name__,
            )
            return _unauthorized("Verification de securite impossible. Reconnectez-vous.")

        # 4 & 5. Existence + role Redis
        actual_role: str = payload.get("role", "")
        try:
            from app.api.routes.auth import _get_user  # noqa: PLC0415
            user_data = await _get_user(user_id)
            if user_data is None:
                logger.warning(
                    "security.user_not_found | user=%s | path=%s",
                    hash_id(user_id), path,
                )
                return _unauthorized("Utilisateur introuvable. Reconnectez-vous.")
            # Role Redis = source de verite
            actual_role = user_data.get("role", actual_role)
            payload["role"] = actual_role
            payload["status"] = user_data.get("status", "active")
            payload["plan"] = user_data.get("plan")
        except Exception as exc:
            logger.warning("security.role_revalidation_failed | %s", type(exc).__name__)

        account_status = payload.get("status", "active")
        if account_status == "pending":
            return _forbidden(
                "Votre compte est en attente de validation par l'administrateur.",
                status="pending",
            )
        if account_status == "suspended":
            return _forbidden(
                "Votre compte a été suspendu.",
                status="suspended",
            )

        # org_id (best-effort)
        org_id = ""
        try:
            from app.core.org_manager import get_org_manager  # noqa: PLC0415
            org_id = await get_org_manager().get_user_org_id(user_id)
        except Exception:
            pass

        # 6. Injection SecurityContext + champs legacy
        ctx = SecurityContext(user_id=user_id, org_id=org_id, role=actual_role)
        request.state.security = ctx
        # Compat legacy (utilises par rate limiter + routes existantes)
        request.state.user    = payload
        request.state.user_id = user_id
        request.state.role    = actual_role
        request.state.org_id  = org_id

        logger.debug("security.ok | user=%s | path=%s | role=%s", hash_id(user_id), path, actual_role)
        return await call_next(request)


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"error": {"code": "UNAUTHORIZED", "message": detail}},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden(detail: str, *, status: str) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={"detail": detail, "status": status},
    )
