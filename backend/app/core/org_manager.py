"""
OrgManager -- gestion des organisations multi-utilisateurs via Redis.

Schema Redis :
  org:data:{org_id}               JSON (Organization)
  org:members:{org_id}            Set de user_ids actifs
  org:member:{org_id}:{user_id}   JSON (OrgMember)
  org:settings:{org_id}           JSON (OrgSettings)
  user:org:{user_id}              org_id (string)
  org:invite:{token}              JSON {org_id, email, role}, TTL 7 jours

Fail-closed :
  - aucun fallback memoire
  - si Redis est indisponible, le circuit breaker ouvre et les appels
    remontent explicitement "Service temporairement indisponible"
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

import redis.asyncio as aioredis
from fastapi import HTTPException, status

from app.api.exception_handlers import ServiceUnavailableError
from app.config import settings
from app.core.result import Result, err, ok
from app.models.organization import OrgMember, OrgSettings, Organization
from app.utils.logger import get_logger, hash_id as _h

logger = get_logger(__name__)


class OrgNotFoundError(Exception):
    """Organisation introuvable ou settings absents."""

_TTL_INVITE = 7 * 24 * 3600
_TTL_ORG = 365 * 24 * 3600
_UNAVAILABLE_MSG = "Service temporairement indisponible"


@dataclass
class _CircuitBreaker:
    FAILURE_THRESHOLD: int = 3
    HALF_OPEN_DELAY: float = 30.0

    _failures: int = field(default=0, init=False)
    _state: str = field(default="CLOSED", init=False)
    _opened_at: float = field(default=0.0, init=False)

    @property
    def state(self) -> str:
        if self._state == "OPEN" and (time.monotonic() - self._opened_at) >= self.HALF_OPEN_DELAY:
            self._state = "HALF_OPEN"
        return self._state

    @property
    def is_open(self) -> bool:
        return self.state == "OPEN"

    def record_success(self) -> None:
        previous = self._state
        self._failures = 0
        self._state = "CLOSED"
        if previous != "CLOSED":
            logger.info("org_manager.circuit CLOSED | previous=%s", previous)

    def record_failure(self) -> None:
        self._failures += 1
        if self._state == "HALF_OPEN" or self._failures >= self.FAILURE_THRESHOLD:
            if self._state != "OPEN":
                logger.error(
                    "org_manager.circuit OPEN | failures=%d | retry_in=%.0fs",
                    self._failures,
                    self.HALF_OPEN_DELAY,
                )
            self._state = "OPEN"
            self._opened_at = time.monotonic()
            return

        logger.warning(
            "org_manager.redis_failure | count=%d/%d | state=CLOSED",
            self._failures,
            self.FAILURE_THRESHOLD,
        )


class OrgManagerUnavailableError(ServiceUnavailableError):
    """OrgManager indisponible car Redis est hors service."""


class OrgManager:
    """Gestion CRUD des organisations avec Redis obligatoire."""

    def __init__(self) -> None:
        self._redis: Optional[aioredis.Redis] = None
        self._redis_url = settings.REDIS_URL
        self._cb = _CircuitBreaker()

    async def _get_redis_result(self) -> Result:
        if self._cb.is_open:
            return err(_UNAVAILABLE_MSG, "ORG_MANAGER_UNAVAILABLE")

        try:
            if self._redis is None:
                self._redis = aioredis.from_url(
                    self._redis_url,
                    encoding="utf-8",
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
            await self._redis.ping()
            self._cb.record_success()
        except Exception as exc:
            self._cb.record_failure()
            self._redis = None
            logger.warning("org_manager.redis_connect_failed | %s", type(exc).__name__)
            return err(_UNAVAILABLE_MSG, "ORG_MANAGER_UNAVAILABLE")

        return ok(self._redis)

    async def _require_redis(self) -> aioredis.Redis:
        redis_result = await self._get_redis_result()
        if not redis_result.ok:
            raise OrgManagerUnavailableError(redis_result.message)
        return redis_result.value

    async def _exec(self, awaitable):
        try:
            result = await awaitable
            self._cb.record_success()
            return result
        except Exception as exc:
            self._cb.record_failure()
            raise OrgManagerUnavailableError(_UNAVAILABLE_MSG) from exc

    async def create_org(
        self,
        name: str,
        owner_id: str,
        plan: str = "starter",
    ) -> Organization:
        org = Organization(name=name, owner_id=owner_id, plan=plan)
        member = OrgMember(org_id=org.org_id, user_id=owner_id, role="admin")
        redis = await self._require_redis()

        pipe = redis.pipeline()
        pipe.setex(f"org:data:{org.org_id}", _TTL_ORG, org.model_dump_json())
        pipe.setex(f"org:member:{org.org_id}:{owner_id}", _TTL_ORG, member.model_dump_json())
        pipe.sadd(f"org:members:{org.org_id}", owner_id)
        pipe.expire(f"org:members:{org.org_id}", _TTL_ORG)
        pipe.setex(f"user:org:{owner_id}", _TTL_ORG, org.org_id)
        default_settings = OrgSettings(org_id=org.org_id)
        pipe.setex(f"org:settings:{org.org_id}", _TTL_ORG, default_settings.model_dump_json())
        pipe.sadd("index:orgs", org.org_id)
        await self._exec(pipe.execute())

        logger.info("OrgManager.create_org | org_id=%s | owner=%s", _h(org.org_id), _h(owner_id))
        return org

    async def get_org(self, org_id: str) -> Optional[Organization]:
        redis = await self._require_redis()
        raw = await self._exec(redis.get(f"org:data:{org_id}"))
        if not raw:
            return None
        try:
            return Organization.model_validate_json(raw)
        except Exception:
            return None

    async def get_user_org(self, user_id: str) -> Optional[Organization]:
        redis = await self._require_redis()
        org_id = await self._exec(redis.get(f"user:org:{user_id}"))
        if not org_id:
            return None
        return await self.get_org(org_id)

    async def get_user_org_id(self, user_id: str) -> str:
        redis = await self._require_redis()
        return await self._exec(redis.get(f"user:org:{user_id}")) or ""

    async def add_member(
        self,
        org_id: str,
        user_id: str,
        role: str = "member",
    ) -> OrgMember:
        member = OrgMember(org_id=org_id, user_id=user_id, role=role)
        redis = await self._require_redis()

        pipe = redis.pipeline()
        pipe.setex(f"org:member:{org_id}:{user_id}", _TTL_ORG, member.model_dump_json())
        pipe.sadd(f"org:members:{org_id}", user_id)
        pipe.expire(f"org:members:{org_id}", _TTL_ORG)
        pipe.setex(f"user:org:{user_id}", _TTL_ORG, org_id)
        await self._exec(pipe.execute())

        return member

    async def remove_member(self, org_id: str, user_id: str) -> bool:
        redis = await self._require_redis()
        key = f"org:member:{org_id}:{user_id}"
        raw = await self._exec(redis.get(key))
        if not raw:
            return False

        try:
            member = OrgMember.model_validate_json(raw)
        except Exception:
            return False

        member.is_active = False
        suspended_ttl = settings.TOKEN_EXPIRE_MINUTES * 60

        pipe = redis.pipeline()
        pipe.setex(key, _TTL_ORG, member.model_dump_json())
        pipe.srem(f"org:members:{org_id}", user_id)
        pipe.delete(f"user:org:{user_id}")
        pipe.setex(f"suspended:{user_id}", suspended_ttl, "1")
        await self._exec(pipe.execute())

        logger.info(
            "OrgManager.remove_member | org=%s | user=%s | suspended_ttl=%ds",
            _h(org_id),
            _h(user_id),
            suspended_ttl,
        )
        return True

    async def get_member(self, org_id: str, user_id: str) -> Optional[OrgMember]:
        redis = await self._require_redis()
        raw = await self._exec(redis.get(f"org:member:{org_id}:{user_id}"))
        if not raw:
            return None
        try:
            return OrgMember.model_validate_json(raw)
        except Exception:
            return None

    async def get_members(self, org_id: str) -> list[OrgMember]:
        redis = await self._require_redis()
        member_ids = await self._exec(redis.smembers(f"org:members:{org_id}"))

        result: list[OrgMember] = []
        for uid in member_ids:
            member = await self.get_member(org_id, uid)
            if member:
                result.append(member)
        return result

    async def list_members(self, org_id: str) -> list[OrgMember]:
        return await self.get_members(org_id)

    async def is_active_member(self, org_id: str, user_id: str) -> bool:
        member = await self.get_member(org_id, user_id)
        return member is not None and member.is_active

    async def create_invite_token(
        self,
        org_id: str,
        email: str,
        role: str = "member",
        invited_by: str = "",
    ) -> str:
        token = secrets.token_urlsafe(32)
        data = json.dumps({
            "org_id": org_id,
            "email": email,
            "role": role,
            "invited_by": invited_by,
            "created_at": time.time(),
            "status": "pending",
        })
        redis = await self._require_redis()
        await self._exec(redis.setex(f"org:invite:{token}", _TTL_INVITE, data))
        return token

    async def get_invite(self, token: str) -> Optional[dict]:
        redis = await self._require_redis()
        raw = await self._exec(redis.get(f"org:invite:{token}"))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    async def validate_invite(self, token: str, email: str) -> dict:
        if not email:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="L'email est obligatoire pour valider l'invitation.",
            )
        invite = await self.get_invite(token)
        if not invite:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invite_token invalide ou expire.",
            )
        invite_email = invite.get("email", "")
        if not invite_email:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token d'invitation invalide.",
            )
        if invite_email.lower() != email.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Ce lien d'invitation ne vous est pas destine.",
            )
        return invite

    async def consume_invite(self, token: str, email: str) -> dict:
        redis = await self._require_redis()
        raw = await self._exec(redis.execute_command("GETDEL", f"org:invite:{token}"))
        if not raw:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invite_token invalide ou deja consomme.",
            )
        try:
            invite = json.loads(raw)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token d'invitation invalide.",
            ) from exc

        invite_email = invite.get("email", "")
        if not invite_email or invite_email.lower() != email.lower():
            await self.restore_invite(token, invite)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Ce lien d'invitation ne vous est pas destine.",
            )

        logger.info(
            "OrgManager.consume_invite | org_id=%s | email=%s | role=%s",
            _h(invite["org_id"]),
            _h(email),
            invite.get("role", "member"),
        )
        return invite

    async def restore_invite(self, token: str, invite: dict) -> None:
        redis = await self._require_redis()
        await self._exec(redis.setex(f"org:invite:{token}", _TTL_INVITE, json.dumps(invite)))

    async def get_settings(self, org_id: str) -> OrgSettings:
        redis = await self._require_redis()
        raw = await self._exec(redis.get(f"org:settings:{org_id}"))
        if not raw:
            raise OrgNotFoundError(f"Organisation {org_id[:8]}... introuvable")
        try:
            return OrgSettings.model_validate_json(raw)
        except Exception as exc:
            raise OrgNotFoundError(f"Organisation {org_id[:8]}... introuvable") from exc

    async def update_settings(self, org_id: str, updates: dict) -> OrgSettings:
        current = await self.get_settings(org_id)
        data = current.model_dump()
        data.update({k: v for k, v in updates.items() if v is not None})
        updated = OrgSettings(**data)

        redis = await self._require_redis()
        await self._exec(redis.setex(f"org:settings:{org_id}", _TTL_ORG, updated.model_dump_json()))
        return updated


_org_manager: Optional[OrgManager] = None


def get_org_manager() -> OrgManager:
    global _org_manager
    if _org_manager is None:
        _org_manager = OrgManager()
    return _org_manager
