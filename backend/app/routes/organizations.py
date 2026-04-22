"""
Routes de gestion des organisations multi-utilisateurs.

POST   /organizations              — cree une organisation (owner=admin)
GET    /organizations/me           — organisation de l'user connecte
POST   /organizations/invite       — genere un token d'invitation
GET    /organizations/members      — liste membres + usage individuel
DELETE /organizations/members/{user_id} — retire un membre
PUT    /organizations/settings     — modifie les parametres RGPD de l'org
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.routes.auth import TokenData, get_current_user
from app.core.org_manager import OrgNotFoundError, get_org_manager
from app.core.usage_tracker import get_monthly_usage
from app.middleware.rate_limit import limiter
from app.models.organization import (
    CreateOrgRequest,
    InviteRequest,
    MemberWithUsage,
    UpdateSettingsRequest,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

import hashlib


def _h(v: str) -> str:
    """Hash a log identifier for GDPR compliance — never log raw PII."""
    return hashlib.sha256(str(v).encode()).hexdigest()[:12]
router = APIRouter()


async def _require_admin(
    current_user: TokenData = Depends(get_current_user),
) -> TokenData:
    """
    Dependance FastAPI — verifie le role admin depuis Redis via get_current_user().
    Utilisation : current_user: TokenData = Depends(_require_admin)
    Le role est toujours recharge depuis Redis par get_current_user(), jamais lu
    depuis le JWT.
    """
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acces reserve aux administrateurs.",
        )
    return current_user


# ---------------------------------------------------------------------------
# POST /organizations
# ---------------------------------------------------------------------------

@router.post("/organizations", status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def create_organization(
    request: Request,
    body: CreateOrgRequest,
    current_user: TokenData = Depends(get_current_user),
):
    """Cree une nouvelle organisation. L'appelant devient owner/admin."""
    mgr = get_org_manager()

    # Verifier si l'user a deja une org
    existing = await mgr.get_user_org(current_user.username)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Vous appartenez deja a l'organisation '{existing.name}'.",
        )

    org = await mgr.create_org(
        name=body.name,
        owner_id=current_user.username,
        plan=body.plan,
    )
    logger.info("org.created | org_id=%s | owner=%s", _h(org.org_id), _h(current_user.username))
    return org.model_dump()


# ---------------------------------------------------------------------------
# GET /organizations/me
# ---------------------------------------------------------------------------

@router.get("/organizations/me")
@limiter.limit("60/minute")
async def get_my_org(
    request: Request,
    current_user: TokenData = Depends(get_current_user),
):
    """Retourne l'organisation de l'utilisateur connecte."""
    mgr = get_org_manager()
    org = await mgr.get_user_org(current_user.username)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Aucune organisation associee a votre compte.",
        )
    member = await mgr.get_member(org.org_id, current_user.username)
    members = await mgr.get_members(org.org_id)
    try:
        settings_obj = await mgr.get_settings(org.org_id)
    except OrgNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organisation introuvable ou desactivee. Contactez votre administrateur.",
        ) from exc
    return {
        **org.model_dump(),
        "my_role": member.role if member else "member",
        "member_count": len(members),
        "settings": settings_obj.model_dump(),
    }


# ---------------------------------------------------------------------------
# POST /organizations/invite
# ---------------------------------------------------------------------------

@router.post("/organizations/invite")
@limiter.limit("20/minute")
async def invite_member(
    request: Request,
    body: InviteRequest,
    current_user: TokenData = Depends(_require_admin),
):
    """Genere un token d'invitation valable 7 jours."""
    mgr = get_org_manager()
    org = await mgr.get_user_org(current_user.username)
    if not org:
        raise HTTPException(status_code=404, detail="Aucune organisation trouvee.")

    token = await mgr.create_invite_token(org.org_id, body.email, body.role)
    logger.info("org.invite | org_id=%s | by=%s", _h(org.org_id), _h(current_user.username))
    return {
        "token": token,
        "org_id": org.org_id,
        "org_name": org.name,
        "email": body.email,
        "role": body.role,
        "expires_in_days": 7,
        "message": f"Partagez ce token pour que {body.email} rejoigne l'organisation.",
    }


# ---------------------------------------------------------------------------
# GET /organizations/members
# ---------------------------------------------------------------------------

@router.get("/organizations/members")
@limiter.limit("30/minute")
async def list_members(
    request: Request,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Liste les membres de l'organisation avec leur usage mensuel.

    Controle d'acces :
      - admin : liste complete de tous les membres avec leurs usages respectifs
      - member : uniquement ses propres donnees (pas d'acces aux donnees des autres)
    """
    mgr = get_org_manager()
    org = await mgr.get_user_org(current_user.username)
    if not org:
        raise HTTPException(status_code=404, detail="Aucune organisation trouvee.")

    if current_user.role == "admin":
        # Admin : liste complete
        members = await mgr.get_members(org.org_id)
        result = []
        for m in members:
            usage = await get_monthly_usage(m.user_id)
            result.append(MemberWithUsage(
                user_id=m.user_id,
                role=m.role,
                joined_at=m.joined_at,
                is_active=m.is_active,
                total_tokens=usage.total_tokens,
                total_cost_usd=usage.total_cost_usd,
                requests_count=usage.requests_count,
            ).model_dump())
        logger.debug(
            "org.members.list | org_id=%s | by=%s | count=%d",
            _h(org.org_id), _h(current_user.username), len(result),
        )
        return {
            "org_id": org.org_id,
            "org_name": org.name,
            "members": result,
            "total": len(result),
        }

    else:
        # Member : uniquement ses propres donnees
        usage = await get_monthly_usage(current_user.username)
        member = await mgr.get_member(org.org_id, current_user.username)
        own_entry = MemberWithUsage(
            user_id=current_user.username,
            role=member.role if member else "member",
            joined_at=member.joined_at if member else None,
            is_active=member.is_active if member else True,
            total_tokens=usage.total_tokens,
            total_cost_usd=usage.total_cost_usd,
            requests_count=usage.requests_count,
        ).model_dump()
        return {
            "org_id": org.org_id,
            "org_name": org.name,
            "members": [own_entry],
            "total": 1,
        }


# ---------------------------------------------------------------------------
# DELETE /organizations/members/{user_id}
# ---------------------------------------------------------------------------

@router.delete("/organizations/members/{target_user_id}")
@limiter.limit("20/minute")
async def remove_member(
    request: Request,
    target_user_id: str,
    current_user: TokenData = Depends(_require_admin),
):
    """Retire un membre de l'organisation (admin uniquement)."""
    mgr = get_org_manager()
    org = await mgr.get_user_org(current_user.username)
    if not org:
        raise HTTPException(status_code=404, detail="Organisation introuvable.")

    if target_user_id == org.owner_id:
        raise HTTPException(status_code=403, detail="Impossible de retirer le proprietaire.")

    removed = await mgr.remove_member(org.org_id, target_user_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Membre introuvable.")

    logger.info("org.member_removed | org_id=%s | user=%s | by=%s", _h(org.org_id), _h(target_user_id), _h(current_user.username))
    return {"removed": True, "user_id": target_user_id}


# ---------------------------------------------------------------------------
# PUT /organizations/settings
# ---------------------------------------------------------------------------

@router.put("/organizations/settings")
@limiter.limit("20/minute")
async def update_settings(
    request: Request,
    body: UpdateSettingsRequest,
    current_user: TokenData = Depends(_require_admin),
):
    """Modifie les parametres RGPD de l'organisation."""
    mgr = get_org_manager()
    org = await mgr.get_user_org(current_user.username)
    if not org:
        raise HTTPException(status_code=404, detail="Organisation introuvable.")

    updates = body.model_dump(exclude_none=True)
    departments_strict_mode = updates.pop("departments_strict_mode", None)
    if departments_strict_mode is not None:
        current = await mgr.get_settings(org.org_id)
        policy = current.policy.model_dump()
        policy["departments_strict_mode"] = departments_strict_mode
        updates["policy"] = policy

    updated = await mgr.update_settings(org.org_id, updates)
    logger.info("org.settings_updated | org_id=%s | by=%s", _h(org.org_id), _h(current_user.username))
    return updated.model_dump()
