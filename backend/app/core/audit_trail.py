from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

import redis.asyncio as aioredis

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

_AUDIT_TTL_SECONDS = 90 * 24 * 3600
_redis_client: Optional[aioredis.Redis] = None


def h(value: Optional[str]) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12] if value else "null"


@dataclass
class AuditEvent:
    event_id: str
    timestamp: str
    user_hash: str
    org_hash: str
    project_hash: str
    action: str
    model_used: str
    protection_mode: str
    file_type: Optional[str]
    entities_masked: int
    cost_usd: float
    success: bool
    error_code: Optional[str]

    @classmethod
    def build(
        cls,
        *,
        user_id: Optional[str],
        org_id: Optional[str],
        project_id: Optional[str],
        action: str,
        model_used: Optional[str],
        protection_mode: Optional[str],
        entities_masked: int,
        file_type: Optional[str],
        cost_usd: float = 0.0,
        success: bool,
        error_code: Optional[str] = None,
    ) -> "AuditEvent":
        return cls(
            event_id=str(uuid4()),
            timestamp=datetime.now(timezone.utc).isoformat(),
            user_hash=h(user_id),
            org_hash=h(org_id),
            project_hash=h(project_id),
            action=action,
            model_used=model_used or "",
            protection_mode=protection_mode or "",
            file_type=file_type,
            entities_masked=entities_masked,
            cost_usd=round(float(cost_usd or 0.0), 6),
            success=success,
            error_code=error_code,
        )


class AuditTrail:
    def __init__(self, redis_client: Optional[aioredis.Redis] = None) -> None:
        self.redis = redis_client
        self.TTL = _AUDIT_TTL_SECONDS

    async def _get_redis(self) -> aioredis.Redis:
        global _redis_client
        if self.redis is not None:
            return self.redis
        if _redis_client is None:
            _redis_client = aioredis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
            )
        self.redis = _redis_client
        return self.redis

    @staticmethod
    def _org_audit_key(org_hash: str) -> str:
        return f"audit:{org_hash}"

    @staticmethod
    def _all_audit_key() -> str:
        return "audit:all"

    @staticmethod
    def _event_score(timestamp: str) -> float:
        try:
            return datetime.fromisoformat(timestamp).timestamp()
        except Exception:
            return datetime.now(timezone.utc).timestamp()

    async def log(self, event: AuditEvent | None = None, /, user_id: Optional[str] = None, org_id: Optional[str] = None,
                  project_id: Optional[str] = None, action: Optional[str] = None, **kwargs) -> None:
        redis = await self._get_redis()
        audit_event = event or AuditEvent.build(
            user_id=user_id,
            org_id=org_id,
            project_id=project_id,
            action=action or "",
            model_used=kwargs.get("model_used"),
            protection_mode=kwargs.get("protection_mode"),
            entities_masked=kwargs.get("entities_masked", 0),
            file_type=kwargs.get("file_type"),
            cost_usd=kwargs.get("cost_usd", 0.0),
            success=kwargs.get("success", True),
            error_code=kwargs.get("error_code"),
        )
        payload = json.dumps(asdict(audit_event))
        audit_key = self._org_audit_key(audit_event.org_hash)
        pipe = redis.pipeline()
        pipe.zadd(audit_key, {payload: self._event_score(audit_event.timestamp)})
        pipe.expire(audit_key, self.TTL)
        pipe.zadd(self._all_audit_key(), {payload: self._event_score(audit_event.timestamp)})
        pipe.expire(self._all_audit_key(), self.TTL)
        await pipe.execute()

    async def get_events(
        self,
        org_id: Optional[str],
        limit: int = 100,
        action: Optional[str] = None,
        user_id: Optional[str] = None,
        date_from: Optional[str] = None,
        model: Optional[str] = None,
    ) -> list[AuditEvent]:
        redis = await self._get_redis()
        org_hash = h(org_id or "")
        hashed_user = h(user_id) if user_id else None
        events: list[AuditEvent] = []
        audit_key = self._org_audit_key(org_hash) if org_id else self._all_audit_key()
        start = 0
        page_size = max(limit * 5, 200)

        while len(events) < limit:
            raw_events = await redis.zrevrange(audit_key, start, start + page_size - 1)
            if not raw_events:
                break

            for raw in raw_events:
                try:
                    payload = json.loads(raw)
                    event = AuditEvent(**payload)
                except Exception:
                    continue

                if action and event.action != action:
                    continue
                if hashed_user and event.user_hash != hashed_user:
                    continue
                if model and event.model_used != model:
                    continue
                if date_from and event.timestamp < date_from:
                    continue
                events.append(event)
                if len(events) >= limit:
                    break

            start += page_size

        return events[:limit]

    async def get_org_events(
        self,
        org_id: str,
        limit: int = 100,
        action: Optional[str] = None,
        date_from: Optional[str] = None,
    ) -> list[dict]:
        events = await self.get_events(
            org_id=org_id,
            limit=limit,
            action=action,
            date_from=date_from,
        )
        return [asdict(event) for event in events]


_audit_trail: Optional[AuditTrail] = None


def get_audit_trail() -> AuditTrail:
    global _audit_trail
    if _audit_trail is None:
        _audit_trail = AuditTrail()
    return _audit_trail
