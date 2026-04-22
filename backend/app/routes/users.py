from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from app.api.routes.auth import (
    TokenData,
    _enforce_password,
    _get_user,
    _set_user,
    get_current_user,
    verify_password,
    pwd_context,
)
from app.config import settings
from app.core.audit_trail import get_audit_trail
from app.core.policy_engine import get_plan_limits
from app.core.project_manager import ProjectManager, VaultDeletionError
from app.core.org_manager import get_org_manager
from app.core.usage_tracker import get_monthly_usage
from app.middleware.rate_limit import limiter
from app.utils.logger import get_logger, hash_id as _h

router = APIRouter()
_pm = ProjectManager()
logger = get_logger(__name__)


class UserProfileResponse(BaseModel):
    username: str
    email: str = ""
    role: str
    status: str = "active"
    plan: Optional[str] = None
    org_id: str = ""
    invited_by: Optional[str] = None
    invitations_used: int = 0
    max_invitations: int = 0
    plan_limits: dict = {}
    created_at: str = ""
    activated_at: Optional[str] = None
    last_login: Optional[str] = None
    is_active: bool = True
    retention_days: int
    tokens_month: int = 0
    provider_api_keys_configured: dict[str, bool] = {}


class UpdateMeRequest(BaseModel):
    email: Optional[str] = None
    provider_api_keys: Optional[dict[str, str]] = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


def _provider_api_keys_status(user: dict | None) -> dict[str, bool]:
    provider_api_keys = (user or {}).get("provider_api_keys") or {}
    return {
        "openai": bool(str(provider_api_keys.get("openai", "") or "").strip()),
        "anthropic": bool(str(provider_api_keys.get("anthropic", "") or "").strip()),
        "google": bool(str(provider_api_keys.get("google", "") or "").strip()),
        "mistral": bool(str(provider_api_keys.get("mistral", "") or "").strip()),
    }


async def _collect_user_usage(user_id: str) -> dict:
    """Collecte les agregats d'usage des 12 derniers mois. Jamais d'exception."""
    try:
        now = datetime.now(timezone.utc)
        months = []
        year, month = now.year, now.month
        for _ in range(12):
            months.append(f"{year:04d}-{month:02d}")
            month -= 1
            if month == 0:
                month, year = 12, year - 1

        aggregates = []
        total_tokens = 0
        total_cost = 0.0
        for ym in months:
            usage = await get_monthly_usage(user_id, ym)
            if hasattr(usage, "model_dump"):
                data = usage.model_dump()
            elif hasattr(usage, "dict"):
                data = usage.dict()
            else:
                data = dict(usage)
            data["month"] = ym
            aggregates.append(data)
            total_tokens += int(data.get("total_tokens", 0) or 0)
            total_cost += float(data.get("total_cost_usd", 0.0) or 0.0)

        return {
            "monthly_aggregates": aggregates,
            "total_tokens_used": total_tokens,
            "total_cost_usd": round(total_cost, 6),
        }
    except Exception as exc:
        logger.warning(
            "export.usage_unavailable | user=%s | %s",
            _h(user_id),
            type(exc).__name__,
        )
        return {
            "monthly_aggregates": [],
            "total_tokens_used": 0,
            "total_cost_usd": 0.0,
            "export_error": "unavailable",
        }


async def _collect_user_audit(user_id: str, limit: int = 1000) -> list:
    """Collecte les entrees d'audit du user. Jamais d'exception."""
    try:
        entries = await get_audit_trail().get_events(
            org_id=None,
            user_id=user_id,
            limit=limit,
        )
        normalized_entries = []
        for entry in entries or []:
            if hasattr(entry, "model_dump"):
                normalized_entries.append(entry.model_dump())
            elif hasattr(entry, "__dict__"):
                normalized_entries.append(dict(entry.__dict__))
            else:
                normalized_entries.append(dict(entry))
        return normalized_entries
    except Exception as exc:
        logger.warning(
            "export.audit_unavailable | user=%s | %s",
            _h(user_id),
            type(exc).__name__,
        )
        return []


@router.get("/users/me", response_model=UserProfileResponse)
@limiter.limit("60/minute")
async def get_me_profile(
    request: Request,
    current_user: TokenData = Depends(get_current_user),
) -> UserProfileResponse:
    user = await _get_user(current_user.username)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Utilisateur introuvable.")
    plan_limits = get_plan_limits(user.get("plan"))
    usage = await get_monthly_usage(current_user.username)

    return UserProfileResponse(
        username=current_user.username,
        email=user.get("email", ""),
        role=user.get("role", current_user.role or "member"),
        status=user.get("status", current_user.status or "active"),
        plan=user.get("plan"),
        org_id=user.get("org_id", current_user.org_id or ""),
        invited_by=user.get("invited_by"),
        invitations_used=int(user.get("invitations_used", 0) or 0),
        max_invitations=int(plan_limits.get("max_invitations", 0) or 0),
        plan_limits=plan_limits,
        created_at=user.get("created_at", ""),
        activated_at=user.get("activated_at"),
        last_login=user.get("last_login"),
        is_active=user.get("is_active", True),
        retention_days=settings.PROJECT_TTL_DAYS,
        tokens_month=usage.total_tokens,
        provider_api_keys_configured=_provider_api_keys_status(user),
    )


@router.patch("/users/me", response_model=UserProfileResponse)
@limiter.limit("30/minute")
async def update_me(
    request: Request,
    body: UpdateMeRequest,
    current_user: TokenData = Depends(get_current_user),
) -> UserProfileResponse:
    user = await _get_user(current_user.username)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Utilisateur introuvable.")

    if body.email is not None:
        email = (body.email or "").strip()
        if not email or "@" not in email or "." not in email.split("@")[-1]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Adresse email invalide.",
            )
        user["email"] = email

    if body.provider_api_keys is not None:
        current_keys = dict(user.get("provider_api_keys") or {})
        for provider in ("openai", "anthropic", "google", "mistral"):
            if provider in body.provider_api_keys:
                current_keys[provider] = str(body.provider_api_keys.get(provider, "") or "").strip()
        user["provider_api_keys"] = current_keys

    await _set_user(current_user.username, user)

    return UserProfileResponse(
        username=current_user.username,
        email=user.get("email", ""),
        role=user.get("role", current_user.role or "member"),
        status=user.get("status", current_user.status or "active"),
        plan=user.get("plan"),
        org_id=user.get("org_id", current_user.org_id or ""),
        invited_by=user.get("invited_by"),
        invitations_used=int(user.get("invitations_used", 0) or 0),
        max_invitations=int(get_plan_limits(user.get("plan")).get("max_invitations", 0) or 0),
        plan_limits=get_plan_limits(user.get("plan")),
        created_at=user.get("created_at", ""),
        activated_at=user.get("activated_at"),
        last_login=user.get("last_login"),
        is_active=user.get("is_active", True),
        retention_days=settings.PROJECT_TTL_DAYS,
        tokens_month=(await get_monthly_usage(current_user.username)).total_tokens,
        provider_api_keys_configured=_provider_api_keys_status(user),
    )


@router.post("/users/me/change-password")
@limiter.limit("20/minute")
async def change_password(
    request: Request,
    body: ChangePasswordRequest,
    current_user: TokenData = Depends(get_current_user),
):
    if body.new_password != body.confirm_password:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="La confirmation du nouveau mot de passe ne correspond pas.",
        )

    user = await _get_user(current_user.username)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Utilisateur introuvable.")

    if not verify_password(body.current_password, user.get("hashed_password", "")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Ancien mot de passe incorrect.",
        )

    _enforce_password(body.new_password)
    user["hashed_password"] = pwd_context.hash(body.new_password)
    await _set_user(current_user.username, user)
    return {"detail": "Mot de passe mis a jour."}


@router.get("/users/me/export")
@limiter.limit("10/minute")
async def export_my_data(
    request: Request,
    current_user: TokenData = Depends(get_current_user),
):
    username = current_user.username
    user = await _get_user(username)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Utilisateur introuvable.")

    projects = []
    for summary in await _pm.list_projects(username):
        project = await _pm.get_project(summary.project_id, username)
        if not project:
            continue
        payload = project.model_dump()
        payload["messages"] = [
            {
                "role": message["role"],
                "content_redacted": message["content_redacted"],
                "timestamp": message["timestamp"],
            }
            for message in payload.get("messages", [])
        ]
        projects.append(payload)

    org_name = ""
    org_id = user.get("org_id", "")
    if org_id:
        org = await get_org_manager().get_org(org_id)
        if org:
            org_name = org.name

    usage_data = await _collect_user_usage(username)
    audit_data = await _collect_user_audit(username)
    export_payload = {
        "user": {
            "username": username,
            "email": user.get("email", ""),
            "role": user.get("role", current_user.role or "member"),
            "org_id": org_id,
            "org_name": org_name,
            "created_at": user.get("created_at", ""),
        },
        "retention_days": settings.PROJECT_TTL_DAYS,
        "projects": projects,
        "usage": usage_data,
        "audit_trail": audit_data,
        "rgpd_export_metadata": {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "article_20_rgpd": True,
            "retention_policy": {
                "vault_ttl_hours": 1,
                "project_archive_after_days": 30,
                "usage_records_ttl_days": 90,
                "usage_aggregates_ttl_days": 365,
                "audit_trail_ttl_days": 730,
            },
        },
    }

    filename = f"{username}_privacy_proxy_export.json"
    return Response(
        content=json.dumps(export_payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/users/me/projects")
@limiter.limit("10/minute")
async def delete_my_projects(
    request: Request,
    current_user: TokenData = Depends(get_current_user),
):
    deleted = 0
    failures: list[str] = []

    for summary in await _pm.list_projects(current_user.username):
        try:
            removed = await _pm.delete_project(summary.project_id, current_user.username)
        except VaultDeletionError:
            failures.append(summary.project_id)
            continue
        if removed:
            deleted += 1
        else:
            failures.append(summary.project_id)

    return {
        "deleted_projects": deleted,
        "failed_projects": failures,
    }
