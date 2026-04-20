"""
Routes de gestion des sessions vault.

GET    /session/{session_id} : statut de la session (existence, nb tokens, backend)
DELETE /session/{session_id} : suppression définitive du vault (RGPD Art. 17)

Authentification : JWT Bearer via Depends(get_current_user)
Rate limit       : 60 req/min

Logs : session_id + user — aucune donnée PII exposée.
"""

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.api.routes.auth import TokenData, get_current_user
from app.core.vault import Vault
from app.middleware.rate_limit import limiter
from app.utils.logger import get_logger, hash_id as _h

logger = get_logger(__name__)
router = APIRouter()

_vault = Vault()

# UUID v4 : 32 hex + 4 tirets = 36 caractères
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Schémas
# ---------------------------------------------------------------------------

class SessionStatus(BaseModel):
    session_id: str
    exists: bool
    token_count: Optional[int] = None
    backend: Optional[str] = None


class SessionDeleted(BaseModel):
    session_id: str
    deleted: bool
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_session_id(session_id: str) -> None:
    """Lève HTTP 400 si le session_id n'est pas un UUID v4 valide."""
    if not _UUID_RE.match(session_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Format de session_id invalide. "
                "Un UUID v4 est attendu (ex: 550e8400-e29b-41d4-a716-446655440000)."
            ),
        )


# ---------------------------------------------------------------------------
# GET /session/{session_id}
# ---------------------------------------------------------------------------

@router.get(
    "/session/{session_id}",
    response_model=SessionStatus,
    status_code=status.HTTP_200_OK,
    summary="Statut d'une session vault",
    description=(
        "Vérifie si la session existe dans le vault et retourne le nombre "
        "de tokens PII stockés. Retourne exists=false si la session est inconnue "
        "ou expirée (TTL dépassé)."
    ),
)
@limiter.limit("60/minute")
async def get_session(
    request: Request,
    session_id: str,
    current_user: TokenData = Depends(get_current_user),
) -> SessionStatus:
    _validate_session_id(session_id)

    # La clé vault:{user_id}:{session_id} n'existe que si ce user est propriétaire
    mapping = await _vault.get_mapping(session_id, user_id=current_user.username)

    if mapping is None:
        logger.debug(
            "session.get | session=%s | user=%s | exists=false",
            _h(session_id), _h(current_user.username),
        )
        return SessionStatus(session_id=session_id, exists=False)

    health = await _vault.health_check()
    logger.debug(
        "session.get | session=%s | user=%s | tokens=%d | backend=%s",
        _h(session_id), _h(current_user.username), len(mapping), health.get("backend"),
    )

    return SessionStatus(
        session_id=session_id,
        exists=True,
        token_count=len(mapping),
        backend=health.get("backend"),
    )


# ---------------------------------------------------------------------------
# DELETE /session/{session_id}
# ---------------------------------------------------------------------------

@router.delete(
    "/session/{session_id}",
    response_model=SessionDeleted,
    status_code=status.HTTP_200_OK,
    summary="Suppression d'une session vault (RGPD Art. 17)",
    description=(
        "Supprime définitivement tous les mappings PII associés à la session. "
        "Implémente le droit à l'effacement (RGPD Article 17). "
        "Opération irréversible — aucune restauration possible après suppression."
    ),
)
@limiter.limit("60/minute")
async def delete_session(
    request: Request,
    session_id: str,
    current_user: TokenData = Depends(get_current_user),
) -> SessionDeleted:
    _validate_session_id(session_id)

    # Vérifie l'appartenance via la clé namespaced vault:{user_id}:{session_id}
    deleted = await _vault.delete_mapping(session_id, user_id=current_user.username)

    logger.info(
        "session.delete | session=%s | user=%s | deleted=%s",
        _h(session_id), _h(current_user.username), deleted,
    )

    return SessionDeleted(
        session_id=session_id,
        deleted=deleted,
        message=(
            "Session supprimée avec succès (droit à l'effacement RGPD appliqué)."
            if deleted
            else "Session introuvable — déjà expirée ou inexistante."
        ),
    )
