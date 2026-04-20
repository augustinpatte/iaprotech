"""
Authentication middleware — valide le JWT Bearer sur chaque requete,
sauf les chemins publics.

Routes publiques (sans authentification) :
  - /health
  - /docs, /openapi.json, /redoc
  - /api/auth/token     (login)
  - /api/auth/register
  - /api/auth/status    (bootstrap check — appele avant tout login)
  - /api/auth/bootstrap (creation du premier compte admin)

Injections dans request.state apres validation reussie :
  - request.state.user    : payload JWT complet (dict, role mis a jour depuis Redis)
  - request.state.user_id : valeur de "sub" (username)
  - request.state.role    : role Redis (source de verite, plus recent que le JWT)
  - request.state.org_id  : organisation de l'utilisateur ("" si aucune)

Securite :
  - JTI blacklist verifiee a chaque requete (tokens revoques via /logout)
  - Role revalide depuis Redis : si l'utilisateur n'existe plus -> 401 ;
    si le role a change depuis l'emission du token -> role Redis utilise
  - user_id logge hashe SHA-256 (RGPD Art. 5(1)(f))
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from jose import ExpiredSignatureError, JWTError, jwt

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


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Starlette middleware qui valide le Bearer JWT sur les routes protegees.

    Sur chaque requete authentifiee :
      1. Decode et valide le JWT (signature + expiration)
      2. Verifie que le JTI n'est pas blackliste (revocation via /logout)
      3. Revalide l'existence et le role de l'utilisateur dans Redis
      4. Injecte user_id, role (Redis), org_id dans request.state
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Routes publiques — passe-plat sans verification
        if path in PUBLIC_PATHS or path.startswith("/docs") or request.method == "OPTIONS":
            return await call_next(request)

        # Extraction du Bearer token
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith(_BEARER_PREFIX):
            logger.warning(
                "auth.missing_header | path=%s | method=%s",
                path, request.method,
            )
            return JSONResponse(
                status_code=401,
                content={"detail": "En-tete Authorization manquant ou malformé. Format : Bearer <token>"},
            )

        token = auth_header[len(_BEARER_PREFIX):]

        # Decodage et validation JWT
        try:
            payload: dict = jwt.decode(
                token,
                settings.SECRET_KEY,
                algorithms=[settings.ALGORITHM],
            )
        except ExpiredSignatureError:
            logger.info("auth.expired | path=%s", path)
            return JSONResponse(
                status_code=401,
                content={"detail": "Token JWT expiré. Veuillez vous reconnecter."},
            )
        except JWTError as exc:
            logger.warning("auth.invalid | path=%s | %s", path, type(exc).__name__)
            return JSONResponse(
                status_code=401,
                content={"detail": "Token JWT invalide."},
            )

        user_id: str = payload.get("sub", "")
        if not user_id:
            logger.warning("auth.no_sub | path=%s", path)
            return JSONResponse(
                status_code=401,
                content={"detail": "Token JWT invalide : claim 'sub' manquant."},
            )

        # JTI blacklist check — token revoque via /logout ?
        jti = payload.get("jti")
        if jti:
            try:
                from app.api.routes.auth import _get_redis as _auth_redis  # noqa: PLC0415
                r = await _auth_redis()
                if r and await r.exists(f"jti_bl:{jti}"):
                    logger.info("auth.revoked | path=%s | user=%s", path, hash_id(user_id))
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "Token révoqué. Veuillez vous reconnecter."},
                    )
            except Exception as exc:
                # Non-bloquant : si Redis est down la blacklist ne peut pas etre verifiee
                logger.warning("auth.jti_check_failed | %s", type(exc).__name__)

        # Revalidation role + existence utilisateur depuis Redis
        # Si l'utilisateur n'existe plus -> 401 (compte supprime/desactive)
        # Si le role a change depuis l'emission du token -> role Redis utilise
        actual_role: str = payload.get("role", "")
        try:
            from app.api.routes.auth import _get_user  # noqa: PLC0415
            user_data = await _get_user(user_id)
            if user_data is None:
                logger.warning(
                    "auth.user_not_found | path=%s | user=%s", path, hash_id(user_id)
                )
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Utilisateur introuvable. Veuillez vous reconnecter."},
                )
            # Role Redis = source de verite (peut avoir change depuis l'emission)
            actual_role = user_data.get("role", actual_role)
            payload["role"] = actual_role
        except Exception as exc:
            # Si Redis est down : fallback au role du token (mode degrade logge)
            logger.warning("auth.role_revalidation_failed | %s", type(exc).__name__)

        # Injection dans request.state
        request.state.user    = payload
        request.state.user_id = user_id   # utilise par le rate limiter
        request.state.role    = actual_role  # role Redis, pour les checks admin

        # org_id (best-effort)
        try:
            from app.core.org_manager import get_org_manager  # noqa: PLC0415
            _mgr = get_org_manager()
            request.state.org_id = await _mgr.get_user_org_id(user_id)
        except Exception:
            request.state.org_id = ""

        logger.debug("auth.ok | user=%s | path=%s", hash_id(user_id), path)
        return await call_next(request)
