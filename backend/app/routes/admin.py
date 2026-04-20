"""
Admin routes -- gestion des utilisateurs, organisations, stats et maintenance.
Toutes ces routes exigent role='admin' via Depends(_require_admin).
"""

import csv
import json
from dataclasses import asdict
from datetime import datetime, timezone
from io import StringIO
from typing import Literal, Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.routes.auth import TokenData, _get_user, _set_user, get_current_user
from app.config import settings
from app.core.audit_trail import get_audit_trail
from app.core.org_manager import get_org_manager
from app.core.policy_engine import PLAN_LIMITS, get_plan_limits
from app.core.usage_tracker import get_monthly_usage
from app.middleware.rate_limit import limiter
from app.utils.logger import get_logger, hash_id as _h

logger = get_logger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------

_redis_client: Optional[aioredis.Redis] = None


async def _get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(
            settings.REDIS_URL, encoding="utf-8", decode_responses=True
        )
    return _redis_client


async def scan_keys(redis: aioredis.Redis, pattern: str, count: int = 100) -> list[str]:
    keys: list[str] = []
    cursor = 0
    while True:
        cursor, batch = await redis.scan(cursor, match=pattern, count=count)
        keys.extend(batch)
        if cursor == 0:
            break
    return keys


async def _paginate_set_members(
    redis: aioredis.Redis,
    key: str,
    *,
    page: int,
    limit: int,
) -> tuple[list[str], int, int]:
    members = sorted(
        member.decode("utf-8") if isinstance(member, bytes) else str(member)
        for member in (await redis.smembers(key))
    )
    total = len(members)
    pages = max(1, (total + limit - 1) // limit) if total else 1
    current_page = max(1, min(page, pages))
    start = (current_page - 1) * limit
    end = start + limit
    return members[start:end], total, pages


async def _get_usernames(redis: aioredis.Redis) -> list[str]:
    usernames = sorted(
        member.decode("utf-8") if isinstance(member, bytes) else str(member)
        for member in (await redis.smembers("index:users"))
    )
    if usernames:
        return usernames

    keys = await scan_keys(redis, "user:*")
    usernames = sorted(key[5:] for key in keys if key.count(":") == 1)
    if usernames:
        await redis.sadd("index:users", *usernames)
    return usernames


async def _get_org_ids(redis: aioredis.Redis) -> list[str]:
    org_ids = sorted(
        member.decode("utf-8") if isinstance(member, bytes) else str(member)
        for member in (await redis.smembers("index:orgs"))
    )
    if org_ids:
        return org_ids

    keys = await scan_keys(redis, "org:data:*")
    org_ids = sorted(key[9:] for key in keys)
    if org_ids:
        await redis.sadd("index:orgs", *org_ids)
    return org_ids


async def _get_total_projects(redis: aioredis.Redis) -> int:
    total = await redis.scard("index:projects:all")
    if total:
        return int(total)

    project_keys = [key for key in await scan_keys(redis, "project:*") if key.count(":") == 1]
    project_ids = [key.split(":", 1)[1] for key in project_keys]
    if project_ids:
        await redis.sadd("index:projects:all", *project_ids)
    return len(project_ids)


# ---------------------------------------------------------------------------
# Dependency: role admin requis
# ---------------------------------------------------------------------------

async def _require_admin(
    current_user: TokenData = Depends(get_current_user),
) -> TokenData:
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return current_user


def _serialize_user(username: str, data: dict, usage) -> dict:
    return {
        "username": username,
        "email": data.get("email", ""),
        "role": data.get("role", "member"),
        "status": data.get("status", "active"),
        "plan": data.get("plan"),
        "org_id": data.get("org_id", ""),
        "invited_by": data.get("invited_by"),
        "is_active": data.get("is_active", True),
        "created_at": data.get("created_at", ""),
        "activated_at": data.get("activated_at"),
        "last_login": data.get("last_login"),
        "invitations_used": int(data.get("invitations_used", 0) or 0),
        "tokens_month": usage.total_tokens,
        "cost_month": round(usage.total_cost_usd, 4),
        "requests_month": usage.requests_count,
    }


# ---------------------------------------------------------------------------
# GET /admin/users
# ---------------------------------------------------------------------------

@router.get("/users", summary="Liste tous les utilisateurs")
@limiter.limit("30/minute")
async def list_users(
    request: Request,
    page: int = 1,
    limit: int = 50,
    admin: TokenData = Depends(_require_admin),
):
    r = await _get_redis()
    limit = max(1, min(limit, 200))
    all_usernames = await _get_usernames(r)
    total = len(all_usernames)
    pages = max(1, (total + limit - 1) // limit) if total else 1
    page = max(1, min(page, pages))
    start = (page - 1) * limit
    usernames = all_usernames[start:start + limit]
    users = []
    for username in usernames:
        raw = await r.get(f"user:{username}")
        if not raw:
            continue
        data = json.loads(raw)
        usage = await get_monthly_usage(username)
        users.append(_serialize_user(username, data, usage))
    users.sort(key=lambda u: u["created_at"], reverse=True)
    logger.info("admin.list_users | admin=%s | page=%d | count=%d", _h(admin.username), page, len(users))
    return {"users": users, "total": total, "page": page, "pages": pages}


# ---------------------------------------------------------------------------
# POST /admin/users/{username}/suspend
# ---------------------------------------------------------------------------

@router.post("/users/{username}/suspend", summary="Suspendre un utilisateur")
@limiter.limit("30/minute")
async def suspend_user(
    username: str,
    request: Request,
    admin: TokenData = Depends(_require_admin),
):
    r = await _get_redis()
    raw = await r.get(f"user:{username}")
    if not raw:
        raise HTTPException(status_code=404, detail="User not found")
    data = json.loads(raw)
    data["status"] = "suspended"
    data["is_active"] = False
    await _set_user(username, data)
    logger.info("admin.suspend | admin=%s | target=%s", _h(admin.username), _h(username))
    return {"ok": True, "username": username, "is_active": False}


# ---------------------------------------------------------------------------
# POST /admin/users/{username}/unsuspend
# ---------------------------------------------------------------------------

@router.post("/users/{username}/unsuspend", summary="Reactiver un utilisateur")
@limiter.limit("30/minute")
async def unsuspend_user(
    username: str,
    request: Request,
    admin: TokenData = Depends(_require_admin),
):
    r = await _get_redis()
    raw = await r.get(f"user:{username}")
    if not raw:
        raise HTTPException(status_code=404, detail="User not found")
    data = json.loads(raw)
    data["status"] = "active"
    data["is_active"] = True
    await _set_user(username, data)
    logger.info("admin.unsuspend | admin=%s | target=%s", _h(admin.username), _h(username))
    return {"ok": True, "username": username, "is_active": True}


# ---------------------------------------------------------------------------
# DELETE /admin/users/{username} -- RGPD: suppression complete
# ---------------------------------------------------------------------------

class DeleteUserResponse(BaseModel):
    ok: bool
    username: str
    deleted_keys: int


@router.delete(
    "/users/{username}",
    response_model=DeleteUserResponse,
    summary="Supprimer un utilisateur (RGPD)",
)
@limiter.limit("10/minute")
async def delete_user(
    username: str,
    request: Request,
    admin: TokenData = Depends(_require_admin),
):
    if username == admin.username:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    r = await _get_redis()
    deleted = 0
    from app.core.project_manager import ProjectManager, VaultDeletionError  # noqa: PLC0415

    pm = ProjectManager()
    project_summaries = await pm.list_projects(username)
    for project in project_summaries:
        try:
            removed = await pm.delete_project(project.project_id, username)
        except VaultDeletionError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Suppression RGPD incomplete pour le projet {project.project_id}.",
            ) from exc
        if not removed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Impossible de supprimer le projet {project.project_id}.",
            )
        deleted += 1

    user_org_id = ""
    raw_user = await r.get(f"user:{username}")
    if raw_user:
        user_data = json.loads(raw_user)
        user_org_id = user_data.get("org_id") or ""

    deleted += await r.delete(f"user:{username}")
    await r.srem("index:users", username)
    deleted += await r.delete(f"user:org:{username}")
    deleted += await r.delete(f"user_projects:{username}")
    deleted += await r.delete(f"user:jti:{username}")
    if user_org_id:
        deleted += await r.delete(f"org:member:{user_org_id}:{username}")
        await r.srem(f"org:members:{user_org_id}", username)
    usage_month_keys = await scan_keys(r, f"usage:month:{username}:*")
    usage_idx_keys = await scan_keys(r, f"usage:idx:{username}")
    usage_rec_keys = await scan_keys(r, f"usage:rec:{username}:*")
    all_usage = usage_month_keys + usage_idx_keys + usage_rec_keys
    if all_usage:
        deleted += await r.delete(*all_usage)
    logger.info(
        "admin.delete_user | admin=%s | target=%s | deleted=%d",
        _h(admin.username), _h(username), deleted,
    )
    return DeleteUserResponse(ok=True, username=username, deleted_keys=deleted)


# ---------------------------------------------------------------------------
# PUT /admin/users/{username}/role
# ---------------------------------------------------------------------------

class RoleUpdate(BaseModel):
    role: Literal["member", "admin"]


class PlanUpdate(BaseModel):
    plan: Literal["basic", "pro", "max"]


class RejectUserRequest(BaseModel):
    reason: Optional[str] = None


@router.put("/users/{username}/role", summary="Changer le role d'un utilisateur")
@limiter.limit("30/minute")
async def update_user_role(
    username: str,
    body: RoleUpdate,
    request: Request,
    admin: TokenData = Depends(_require_admin),
):
    r = await _get_redis()
    raw = await r.get(f"user:{username}")
    if not raw:
        raise HTTPException(status_code=404, detail="User not found")
    data = json.loads(raw)
    data["role"] = body.role
    await r.set(f"user:{username}", json.dumps(data))
    logger.info(
        "admin.update_role | admin=%s | target=%s | role=%s",
        _h(admin.username), _h(username), body.role,
    )
    return {
        "ok": True,
        "username": username,
        "role": body.role,
        "user": {
            "username": username,
            "role": data.get("role", "member"),
            "org_id": data.get("org_id", ""),
            "is_active": data.get("is_active", True),
            "created_at": data.get("created_at", ""),
        },
    }


@router.get("/users/pending", summary="Liste les comptes en attente")
@limiter.limit("30/minute")
async def list_pending_users(
    request: Request,
    admin: TokenData = Depends(_require_admin),
):
    r = await _get_redis()
    users = []
    for username in await _get_usernames(r):
        user = await _get_user(username)
        if not user or user.get("status") != "pending":
            continue
        users.append({
            "username": username,
            "email": user.get("email", ""),
            "created_at": user.get("created_at", ""),
            "invited_by": user.get("invited_by"),
        })
    users.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return {"users": users, "count": len(users)}


@router.get("/users/all", summary="Liste complete des comptes avec filtres")
@limiter.limit("30/minute")
async def list_all_users(
    request: Request,
    status_filter: Optional[str] = None,
    plan: Optional[str] = None,
    admin: TokenData = Depends(_require_admin),
):
    r = await _get_redis()
    users = []
    for username in await _get_usernames(r):
        user = await _get_user(username)
        if not user:
            continue
        if status_filter and user.get("status") != status_filter:
            continue
        if plan and user.get("plan") != plan:
            continue
        usage = await get_monthly_usage(username)
        users.append(_serialize_user(username, user, usage))
    users.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return {"users": users, "count": len(users)}


@router.post("/users/{username}/activate", summary="Activer un compte pending")
@limiter.limit("20/minute")
async def activate_user(
    username: str,
    body: PlanUpdate,
    request: Request,
    admin: TokenData = Depends(_require_admin),
):
    user = await _get_user(username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    activated_at = datetime.now(timezone.utc).isoformat()
    org_manager = get_org_manager()
    org_id = user.get("org_id") or ""
    invited_by = user.get("invited_by")
    if invited_by:
        inviter = await _get_user(invited_by)
        if inviter and inviter.get("org_id"):
            org_id = inviter.get("org_id", "")
            if not await org_manager.get_member(org_id, username):
                await org_manager.add_member(org_id, username, "member")

    if not org_id:
        org = await org_manager.create_org(
            name=f"Organisation {username}",
            owner_id=username,
            plan=body.plan,
        )
        org_id = org.org_id

    user.update({
        "status": "active",
        "plan": body.plan,
        "org_id": org_id,
        "activated_at": activated_at,
        "is_active": True,
    })
    await _set_user(username, user)
    updated = await _get_user(username)
    usage = await get_monthly_usage(username)
    return {"ok": True, "user": _serialize_user(username, updated or user, usage)}


@router.post("/users/{username}/reject", summary="Refuser un compte pending")
@limiter.limit("20/minute")
async def reject_user(
    username: str,
    body: RejectUserRequest,
    request: Request,
    admin: TokenData = Depends(_require_admin),
):
    user = await _get_user(username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.update({
        "status": "suspended",
        "is_active": False,
        "reject_reason": body.reason,
    })
    await _set_user(username, user)
    return {"ok": True, "username": username, "status": "suspended"}


@router.put("/users/{username}/plan", summary="Changer le plan d'un utilisateur")
@limiter.limit("20/minute")
async def update_user_plan(
    username: str,
    body: PlanUpdate,
    request: Request,
    admin: TokenData = Depends(_require_admin),
):
    user = await _get_user(username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.get("status") != "active":
        raise HTTPException(status_code=409, detail="Seuls les comptes actifs peuvent changer de plan.")
    user["plan"] = body.plan
    await _set_user(username, user)
    usage = await get_monthly_usage(username)
    return {
        "ok": True,
        "plan": body.plan,
        "plan_limits": get_plan_limits(body.plan),
        "user": _serialize_user(username, user, usage),
    }


# ---------------------------------------------------------------------------
# GET /admin/stats
# ---------------------------------------------------------------------------

@router.get("/stats", summary="Statistiques globales")
@limiter.limit("30/minute")
async def global_stats(
    request: Request,
    admin: TokenData = Depends(_require_admin),
):
    r = await _get_redis()
    usernames = await _get_usernames(r)
    total_projects = await _get_total_projects(r)
    total_orgs = len(await _get_org_ids(r))

    total_tokens = 0
    total_cost = 0.0
    total_requests = 0
    for username in usernames:
        usage = await get_monthly_usage(username)
        total_tokens += usage.total_tokens
        total_cost += usage.total_cost_usd
        total_requests += usage.requests_count

    return {
        "total_users": len(usernames),
        "total_projects": total_projects,
        "total_organizations": total_orgs,
        "month_tokens": total_tokens,
        "month_cost_usd": round(total_cost, 4),
        "month_requests": total_requests,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# GET /admin/organizations
# ---------------------------------------------------------------------------

@router.get("/organizations", summary="Liste toutes les organisations")
@limiter.limit("30/minute")
async def list_organizations(
    request: Request,
    page: int = 1,
    limit: int = 50,
    admin: TokenData = Depends(_require_admin),
):
    r = await _get_redis()
    limit = max(1, min(limit, 200))
    all_org_ids = await _get_org_ids(r)
    total = len(all_org_ids)
    pages = max(1, (total + limit - 1) // limit) if total else 1
    page = max(1, min(page, pages))
    start = (page - 1) * limit
    org_ids = all_org_ids[start:start + limit]
    orgs = []
    for org_id in org_ids:
        raw = await r.get(f"org:data:{org_id}")
        if not raw:
            continue
        data = json.loads(raw)
        members = await r.smembers(f"org:members:{org_id}")
        org_tokens = 0
        org_cost = 0.0
        for member in members:
            usage = await get_monthly_usage(member)
            org_tokens += usage.total_tokens
            org_cost += usage.total_cost_usd
        orgs.append({
            "org_id": org_id,
            "name": data.get("name", ""),
            "plan": data.get("plan", ""),
            "owner_id": data.get("owner_id", ""),
            "member_count": len(members),
            "month_tokens": org_tokens,
            "month_cost_usd": round(org_cost, 4),
            "created_at": data.get("created_at", ""),
        })
    orgs.sort(key=lambda o: o["created_at"], reverse=True)
    return {"organizations": orgs, "total": total, "page": page, "pages": pages}


# ---------------------------------------------------------------------------
# DELETE /admin/organizations/{org_id}
# ---------------------------------------------------------------------------

@router.delete("/organizations/{org_id}", summary="Supprimer une organisation")
@limiter.limit("10/minute")
async def delete_organization(
    org_id: str,
    request: Request,
    admin: TokenData = Depends(_require_admin),
):
    r = await _get_redis()
    if not await r.exists(f"org:data:{org_id}"):
        raise HTTPException(status_code=404, detail="Organization not found")
    members = await r.smembers(f"org:members:{org_id}")
    suspended_ttl = max(86400, settings.TOKEN_EXPIRE_MINUTES * 60)
    for member in members:
        raw_user = await r.get(f"user:{member}")
        if raw_user:
            user_data = json.loads(raw_user)
            user_data["org_id"] = None
            user_data["is_active"] = False
            await r.set(f"user:{member}", json.dumps(user_data))

        active_jtis = await r.smembers(f"user:jti:{member}")
        now_ts = int(datetime.now(timezone.utc).timestamp())
        for entry in active_jtis or []:
            entry_str = str(entry)
            try:
                jti, exp_ts_raw = entry_str.rsplit(":", 1)
                remaining = max(1, int(exp_ts_raw) - now_ts)
                await r.setex(f"jti_bl:{jti}", remaining, "org_deleted")
            except Exception:
                logger.warning(
                    "admin.delete_org.revoke_jti WARN | org=%s | user=%s",
                    _h(org_id),
                    _h(member),
                )
        await r.delete(f"user:jti:{member}")
        await r.setex(f"suspended:{member}", suspended_ttl, "org_deleted")
        await r.delete(f"user:org:{member}")
        await r.delete(f"org:member:{org_id}:{member}")
    deleted = 0
    deleted += await r.delete(f"org:data:{org_id}")
    await r.srem("index:orgs", org_id)
    deleted += await r.delete(f"org:members:{org_id}")
    deleted += await r.delete(f"org:settings:{org_id}")
    invite_keys = await scan_keys(r, "org:invite:*")
    for k in invite_keys:
        raw = await r.get(k)
        if raw:
            inv = json.loads(raw)
            if inv.get("org_id") == org_id:
                await r.delete(k)
                deleted += 1
    logger.info(
        "admin.delete_org | admin=%s | org=%s | deleted=%d",
        _h(admin.username), _h(org_id), deleted,
    )
    return {"ok": True, "org_id": org_id, "deleted_keys": deleted}


# ---------------------------------------------------------------------------
# GET /admin/logs
# ---------------------------------------------------------------------------

@router.get("/logs", summary="Derniers 100 evenements d'audit")
@limiter.limit("30/minute")
async def get_logs(
    request: Request,
    admin: TokenData = Depends(_require_admin),
):
    """
    Lit les 100 derniers evenements depuis le fichier de log JSON structure.
    Aucun contenu sensible: identifiants hashs SHA-256.
    """
    from pathlib import Path as _Path
    log_path = _Path("/app/logs/privacy_proxy.log")
    entries = []
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-100:]:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except Exception:
                entries.append({"raw": line[:200]})
    return {"entries": entries, "count": len(entries)}


def _resolve_audit_org_id(admin: TokenData, org_id: Optional[str]) -> Optional[str]:
    return org_id or admin.org_id or None


@router.get("/audit", summary="Derniers evenements d'audit Redis")
@limiter.limit("30/minute")
async def get_audit_events(
    request: Request,
    org_id: Optional[str] = None,
    limit: int = 100,
    action: Optional[str] = None,
    date: Optional[str] = None,
    admin: TokenData = Depends(_require_admin),
):
    effective_org_id = _resolve_audit_org_id(admin, org_id)
    events = await get_audit_trail().get_org_events(
        org_id=effective_org_id or "",
        limit=max(1, min(limit, 100)),
        action=action,
        date_from=date,
    )
    return {"events": events, "count": len(events)}


@router.get("/audit/export", summary="Export CSV des evenements d'audit")
@limiter.limit("10/minute")
async def export_audit_events(
    request: Request,
    org_id: Optional[str] = None,
    limit: int = 100,
    action: Optional[str] = None,
    date: Optional[str] = None,
    admin: TokenData = Depends(_require_admin),
) -> StreamingResponse:
    effective_org_id = _resolve_audit_org_id(admin, org_id)
    events = await get_audit_trail().get_org_events(
        org_id=effective_org_id or "",
        limit=max(1, min(limit, 1000)),
        action=action,
        date_from=date,
    )

    buffer = StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=[
            "event_id",
            "timestamp",
            "user_hash",
            "org_hash",
            "project_hash",
            "action",
            "model_used",
            "protection_mode",
            "file_type",
            "entities_masked",
            "cost_usd",
            "success",
            "error_code",
        ],
    )
    writer.writeheader()
    for event in events:
        writer.writerow(event)

    filename = f"audit_trail_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# POST /admin/maintenance/purge-expired
# ---------------------------------------------------------------------------

@router.post("/maintenance/purge-expired", summary="Purger les sessions vault expirees")
@limiter.limit("5/minute")
async def purge_expired(
    request: Request,
    admin: TokenData = Depends(_require_admin),
):
    """Supprime les cles excel_wb:* sans TTL et recense les cles vault orphelines."""
    r = await _get_redis()
    vault_keys = await scan_keys(r, "vault:*")
    excel_keys = await scan_keys(r, "excel_wb:*")
    purged = 0
    for key in excel_keys:
        ttl = await r.ttl(key)
        if ttl < 0:
            await r.delete(key)
            purged += 1
    logger.info(
        "admin.purge_expired | admin=%s | purged=%d",
        _h(admin.username), purged,
    )
    return {
        "ok": True,
        "vault_keys_found": len(vault_keys),
        "excel_keys_found": len(excel_keys),
        "purged": purged,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
