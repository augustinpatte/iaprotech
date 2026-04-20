"""
Routes de gestion des projets persistants.

GET  /projects                    -- liste tous les projets de l'user
GET  /projects/{project_id}       -- detail + messages
DELETE /projects/{project_id}     -- suppression RGPD
POST /projects/{project_id}/archive -- archivage manuel
POST /projects/archive-inactive   -- archivage auto (admin)

Authentification : JWT Bearer via Depends(get_current_user)
Logs : project_id + user uniquement -- zero contenu sensible.
"""

import re
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.api.routes.auth import TokenData, get_current_user
from app.core.project_manager import ProjectManager
from app.middleware.rate_limit import limiter
from app.models.project import (
    ProjectArchiveResponse,
    ProjectDeletedResponse,
    ProjectDetailResponse,
    ProjectListResponse,
    ProjectMessagePublic,
    ProjectSummary,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

import hashlib


def _h(v: str) -> str:
    """Hash a log identifier for GDPR compliance — never log raw PII."""
    return hashlib.sha256(str(v).encode()).hexdigest()[:12]
router = APIRouter()
_pm = ProjectManager()

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _validate_project_id(project_id: str) -> None:
    if not _UUID_RE.match(project_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Format de project_id invalide. UUID v4 attendu.",
        )


# ---------------------------------------------------------------------------
# GET /projects
# ---------------------------------------------------------------------------

@router.get(
    "/projects",
    response_model=ProjectListResponse,
    summary="Liste tous les projets de l'utilisateur",
)
@limiter.limit("60/minute")
async def list_projects(
    request: Request,
    current_user: TokenData = Depends(get_current_user),
) -> ProjectListResponse:
    summaries = await _pm.list_projects(current_user.username)
    logger.debug(
        "projects.list | user=%s | count=%d", _h(current_user.username), len(summaries)
    )
    return ProjectListResponse(projects=summaries, total=len(summaries))


# ---------------------------------------------------------------------------
# GET /projects/{project_id}
# ---------------------------------------------------------------------------

@router.get(
    "/projects/{project_id}",
    response_model=ProjectDetailResponse,
    summary="Detail d'un projet avec historique de messages",
)
@limiter.limit("60/minute")
async def get_project(
    request: Request,
    project_id: str,
    current_user: TokenData = Depends(get_current_user),
) -> ProjectDetailResponse:
    _validate_project_id(project_id)
    project = await _pm.get_project(project_id, current_user.username)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Projet introuvable ou acces refuse.",
        )
    logger.info(
        "projects.get | project=%s | user=%s | messages=%d",
        _h(project_id), _h(current_user.username), project.messages_count,
    )
    return ProjectDetailResponse(
        project_id=project.project_id,
        name=project.name,
        created_at=project.created_at,
        last_activity=project.last_activity,
        messages_count=project.messages_count,
        status=project.status,
        session_id=project.session_id,
        messages=[
            ProjectMessagePublic(
                role=m.role,
                content_redacted=m.content_redacted,
                timestamp=m.timestamp,
            )
            for m in project.messages
        ],
    )


# ---------------------------------------------------------------------------
# DELETE /projects/{project_id}
# ---------------------------------------------------------------------------

@router.delete(
    "/projects/{project_id}",
    response_model=ProjectDeletedResponse,
    summary="Suppression RGPD d'un projet (Art. 17)",
)
@limiter.limit("30/minute")
async def delete_project(
    request: Request,
    project_id: str,
    current_user: TokenData = Depends(get_current_user),
) -> ProjectDeletedResponse:
    _validate_project_id(project_id)
    # Verifie que le projet appartient a l'utilisateur avant suppression
    owned = await _pm.get_project(project_id, current_user.username)
    if not owned:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Projet introuvable ou acces refuse.",
        )
    deleted = await _pm.delete_project(project_id, current_user.username)
    logger.info(
        "projects.delete | project=%s | user=%s | deleted=%s",
        _h(project_id), _h(current_user.username), deleted,
    )
    return ProjectDeletedResponse(
        project_id=project_id,
        deleted=deleted,
        message=(
            "Projet supprime (droit a l'effacement RGPD applique)."
            if deleted
            else "Projet introuvable ou acces refuse."
        ),
    )


# ---------------------------------------------------------------------------
# POST /projects/{project_id}/archive
# ---------------------------------------------------------------------------

@router.post(
    "/projects/{project_id}/archive",
    response_model=ProjectArchiveResponse,
    summary="Archive manuellement un projet",
)
@limiter.limit("30/minute")
async def archive_project(
    request: Request,
    project_id: str,
    current_user: TokenData = Depends(get_current_user),
) -> ProjectArchiveResponse:
    _validate_project_id(project_id)
    ok = await _pm.archive_project(project_id, current_user.username)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Projet introuvable ou acces refuse.",
        )
    logger.info(
        "projects.archive | project=%s | user=%s", _h(project_id), _h(current_user.username)
    )
    return ProjectArchiveResponse(
        project_id=project_id,
        status="archived",
        message="Projet archive avec succes.",
    )



# ---------------------------------------------------------------------------
# PATCH /projects/{project_id}
# ---------------------------------------------------------------------------

class RenameRequest(BaseModel):
    name: str


@router.patch(
    "/projects/{project_id}",
    response_model=ProjectSummary,
    summary="Renommer un projet",
)
@limiter.limit("60/minute")
async def rename_project(
    request: Request,
    project_id: str,
    body: RenameRequest,
    current_user: TokenData = Depends(get_current_user),
) -> ProjectSummary:
    _validate_project_id(project_id)
    if not body.name or not body.name.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Le nom ne peut pas etre vide.",
        )
    project = await _pm.rename_project(project_id, current_user.username, body.name.strip())
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Projet introuvable ou acces refuse.",
        )
    logger.info(
        "projects.rename | project=%s | user=%s",
        _h(project_id), _h(current_user.username),
    )
    return ProjectSummary(
        project_id=project.project_id,
        name=project.name,
        created_at=project.created_at,
        last_activity=project.last_activity,
        messages_count=project.messages_count,
        status=project.status,
    )

# ---------------------------------------------------------------------------
# POST /projects/archive-inactive  (admin utility)
# ---------------------------------------------------------------------------

@router.post(
    "/projects/archive-inactive",
    summary="Archive tous les projets inactifs depuis 30 jours",
)
@limiter.limit("5/minute")
async def archive_inactive(
    request: Request,
    current_user: TokenData = Depends(get_current_user),
) -> dict:
    # Utilise le role Redis (injecte par le middleware) si disponible
    role = getattr(request.state, "role", None) or current_user.role
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Reserve aux administrateurs",
        )
    count = await _pm.archive_inactive_projects()
    logger.info(
        "projects.archive_inactive | user=%s | archived=%d",
        _h(current_user.username), count,
    )
    return {"archived": count, "message": f"{count} projet(s) archive(s)."}
