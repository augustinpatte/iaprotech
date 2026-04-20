from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.routes.auth import TokenData, _get_redis, _get_user, _set_user, get_current_user
from app.config import settings
from app.core.org_manager import get_org_manager
from app.core.policy_engine import get_plan_limits
from app.middleware.rate_limit import limiter

router = APIRouter()

_INVITE_META_TTL = 30 * 24 * 3600


class SendInvitationRequest(BaseModel):
    email: str = Field(..., max_length=256)
    message: Optional[str] = None


def _require_active_plan_user(user: dict) -> tuple[str, dict]:
    if user.get("status") != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Votre compte doit être activé pour inviter des membres.",
        )

    plan = user.get("plan")
    if not plan:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Vous n'avez pas encore de plan actif. Contactez l'administrateur.",
        )

    limits = get_plan_limits(plan)
    if not limits:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Plan utilisateur invalide.",
        )
    return plan, limits


async def _load_invite_meta(token: str) -> Optional[dict]:
    r = await _get_redis()
    if r is None:
        return None
    raw = await r.get(f"invite:meta:{token}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


@router.post("/invitations/send")
@limiter.limit("20/minute")
async def send_invitation(
    request: Request,
    body: SendInvitationRequest,
    current_user: TokenData = Depends(get_current_user),
):
    user = await _get_user(current_user.username)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Utilisateur introuvable.")

    plan, limits = _require_active_plan_user(user)
    if user.get("invitations_used", 0) >= limits.get("max_invitations", 0):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Quota d'invitations atteint pour votre plan.",
        )

    org = await get_org_manager().get_user_org(current_user.username)
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation introuvable.")

    token = await get_org_manager().create_invite_token(
        org.org_id,
        body.email.strip().lower(),
        role="member",
        invited_by=current_user.username,
    )

    invite_link_base = settings.ALLOWED_ORIGINS[0] if settings.ALLOWED_ORIGINS else "https://iaprotech.com"
    invite_link = invite_link_base.rstrip("/") + f"/register?token={token}"
    meta = {
        "token": token,
        "email": body.email.strip().lower(),
        "message": (body.message or "").strip(),
        "invited_by": current_user.username,
        "org_id": org.org_id,
        "plan": plan,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "invite_link": invite_link,
    }

    r = await _get_redis()
    if r is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Redis indisponible.")
    pipe = r.pipeline()
    pipe.setex(f"invite:meta:{token}", _INVITE_META_TTL, json.dumps(meta))
    pipe.sadd(f"index:invites:{current_user.username}", token)
    await pipe.execute()

    user["invitations_used"] = int(user.get("invitations_used", 0) or 0) + 1
    await _set_user(current_user.username, user)

    return {
        "token": token,
        "invite_link": invite_link,
        "invitations_used": user["invitations_used"],
        "max_invitations": limits.get("max_invitations", 0),
    }


@router.get("/invitations/my")
@limiter.limit("60/minute")
async def my_invitations(
    request: Request,
    current_user: TokenData = Depends(get_current_user),
):
    user = await _get_user(current_user.username)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Utilisateur introuvable.")

    plan, limits = _require_active_plan_user(user)
    r = await _get_redis()
    if r is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Redis indisponible.")

    tokens = sorted(await r.smembers(f"index:invites:{current_user.username}"))
    invitations = []
    for token in tokens:
        meta = await _load_invite_meta(token)
        if meta:
            invitations.append(meta)
    invitations.sort(key=lambda item: item.get("created_at", ""), reverse=True)

    return {
        "plan": plan,
        "invitations_used": int(user.get("invitations_used", 0) or 0),
        "max_invitations": limits.get("max_invitations", 0),
        "items": invitations,
    }


@router.delete("/invitations/{token}")
@limiter.limit("30/minute")
async def cancel_invitation(
    request: Request,
    token: str,
    current_user: TokenData = Depends(get_current_user),
):
    user = await _get_user(current_user.username)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Utilisateur introuvable.")

    meta = await _load_invite_meta(token)
    if not meta or meta.get("invited_by") != current_user.username:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation introuvable.")
    if meta.get("status") != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Invitation déjà utilisée.")

    r = await _get_redis()
    if r is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Redis indisponible.")
    pipe = r.pipeline()
    pipe.delete(f"org:invite:{token}")
    pipe.delete(f"invite:meta:{token}")
    pipe.srem(f"index:invites:{current_user.username}", token)
    await pipe.execute()

    user["invitations_used"] = max(0, int(user.get("invitations_used", 0) or 0) - 1)
    await _set_user(current_user.username, user)
    return {"cancelled": True, "token": token}
