"""
Rate limiter — slowapi (Starlette-compatible).

Limites configurées :
  • Global   : 60 req/min par utilisateur (ou IP en fallback)
  • /upload  : 10 req/min (extraction CPU-intensive)

Headers X-RateLimit-* automatiquement ajoutés par SlowAPIMiddleware.
À enregistrer dans main.py :
    from slowapi.middleware import SlowAPIMiddleware
    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)

Usage dans les routes :
    @limiter.limit(DEFAULT_LIMIT)
    async def my_route(request: Request, ...): ...

    @limiter.limit(UPLOAD_LIMIT)
    async def upload(request: Request, ...): ...
"""

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constantes de limite — importables par les routes
# ---------------------------------------------------------------------------

DEFAULT_LIMIT: str = f"{settings.MAX_REQUESTS_PER_MINUTE}/minute"  # "60/minute"
UPLOAD_LIMIT: str = "10/minute"


# ---------------------------------------------------------------------------
# Fonction de clé — username JWT en priorité, IP en fallback
# ---------------------------------------------------------------------------

def _rate_key(request: Request) -> str:
    """
    Retourne la clé de rate-limit :
      1. username extrait du JWT (via request.state.user_id — injecté par SecurityMiddleware)
      2. IP du client en fallback (pour les routes publiques ou si user_id absent)
    """
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        return f"user:{user_id}"
    return f"ip:{get_remote_address(request)}"


# ---------------------------------------------------------------------------
# Instance Limiter — singleton importé par les routes et main.py
# ---------------------------------------------------------------------------

limiter = Limiter(
    key_func=_rate_key,
    storage_uri=settings.REDIS_URL,
    default_limits=[DEFAULT_LIMIT, "1000/day"],
    # Les headers X-RateLimit-* sont émis par SlowAPIMiddleware (voir main.py)
)
