"""
UsageTracker -- enregistrement et agregation de l'usage LLM par utilisateur.

Schema Redis :
  usage:rec:{user_id}:{ts_ms}     JSON (UsageRecord), TTL 90 jours
  usage:idx:{user_id}             Sorted set (score=ts_ms, member=ts_ms)
  usage:month:{user_id}:{YYYY-MM} Hash (champs atomiques HINCRBY/HINCRBYFLOAT)

Fail-closed :
  - aucun fallback memoire
  - si Redis est indisponible, le circuit breaker ouvre et les lectures
    remontent explicitement "Service temporairement indisponible"
"""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import redis.asyncio as aioredis

from app.api.exception_handlers import ServiceUnavailableError
from app.config import settings
from app.core.result import Result, err, ok
from app.models.usage import MonthlyUsage, UsageRecord
from app.utils.logger import get_logger

logger = get_logger(__name__)

_TTL_RECORD = 90 * 24 * 3600
_TTL_MONTHLY = 365 * 24 * 3600
_UNAVAILABLE_MSG = "Service temporairement indisponible"
PROCESSING_COST_PER_MB = 0.001


def _hash_session(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()[:16]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def estimate_local_processing_cost(file_size_bytes: int) -> float:
    file_size_mb = max(float(file_size_bytes), 0.0) / (1024 * 1024)
    return round(file_size_mb * PROCESSING_COST_PER_MB, 6)


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
            logger.info("usage_tracker.circuit CLOSED | previous=%s", previous)

    def record_failure(self) -> None:
        self._failures += 1
        if self._state == "HALF_OPEN" or self._failures >= self.FAILURE_THRESHOLD:
            if self._state != "OPEN":
                logger.error(
                    "usage_tracker.circuit OPEN | failures=%d | retry_in=%.0fs",
                    self._failures,
                    self.HALF_OPEN_DELAY,
                )
            self._state = "OPEN"
            self._opened_at = time.monotonic()
            return

        logger.warning(
            "usage_tracker.redis_failure | count=%d/%d | state=CLOSED",
            self._failures,
            self.FAILURE_THRESHOLD,
        )


_redis_client: Optional[aioredis.Redis] = None
_cb = _CircuitBreaker()


async def _get_redis_result() -> Result:
    global _redis_client

    if _cb.is_open:
        return err(_UNAVAILABLE_MSG, "USAGE_TRACKER_UNAVAILABLE")

    try:
        if _redis_client is None:
            _redis_client = aioredis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            logger.info("UsageTracker: connexion Redis etablie")
        await _redis_client.ping()
        _cb.record_success()
    except Exception as exc:
        _cb.record_failure()
        _redis_client = None
        logger.warning("UsageTracker: Redis indisponible (%s)", type(exc).__name__)
        return err(_UNAVAILABLE_MSG, "USAGE_TRACKER_UNAVAILABLE")

    return ok(_redis_client)


async def _require_redis() -> aioredis.Redis:
    redis_result = await _get_redis_result()
    if not redis_result.ok:
        raise ServiceUnavailableError(redis_result.message)
    return redis_result.value


async def track_usage(
    user_id: str,
    model: str,
    provider: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    request_type: str,
    session_id: str = "",
    org_id: str = "",
) -> Result:
    """Enregistre un appel LLM dans Redis. Retourne Ok(True) | Err(...)."""
    ts_ms = _now_ms()
    month = _current_month()
    rec_key = f"usage:rec:{user_id}:{ts_ms}"
    idx_key = f"usage:idx:{user_id}"
    mon_key = f"usage:month:{user_id}:{month}"

    record = UsageRecord(
        user_id=user_id,
        session_id_hash=_hash_session(session_id) if session_id else "",
        model_used=model,
        provider=provider,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        timestamp=datetime.now(timezone.utc).isoformat(),
        request_type=request_type,  # type: ignore[arg-type]
        org_id=org_id,
    )
    total_tokens = input_tokens + output_tokens

    redis_result = await _get_redis_result()
    if not redis_result.ok:
        logger.warning("track_usage indisponible | user=%s | reason=%s", user_id, redis_result.message)
        return redis_result

    redis = redis_result.value
    try:
        pipe = redis.pipeline()
        pipe.setex(rec_key, _TTL_RECORD, record.model_dump_json())
        pipe.zadd(idx_key, {str(ts_ms): ts_ms})
        pipe.expire(idx_key, _TTL_RECORD)
        pipe.hincrbyfloat(mon_key, "total_cost_usd", cost_usd)
        pipe.hincrby(mon_key, "total_tokens", total_tokens)
        pipe.hincrby(mon_key, "requests_count", 1)
        pipe.hincrbyfloat(mon_key, f"provider:{provider}:cost_usd", cost_usd)
        pipe.hincrby(mon_key, f"provider:{provider}:tokens", total_tokens)
        pipe.hincrby(mon_key, f"provider:{provider}:count", 1)
        pipe.hincrbyfloat(mon_key, f"model:{model}:cost_usd", cost_usd)
        pipe.hincrby(mon_key, f"model:{model}:tokens", total_tokens)
        pipe.hincrby(mon_key, f"model:{model}:count", 1)
        pipe.expire(mon_key, _TTL_MONTHLY)
        await pipe.execute()
        _cb.record_success()
    except Exception as exc:
        _cb.record_failure()
        logger.warning("track_usage ERREUR | %s", type(exc).__name__)
        return err(_UNAVAILABLE_MSG, "USAGE_TRACKER_UNAVAILABLE")

    logger.debug(
        "track_usage | user=%s | model=%s | tokens=%d | cost=%.6f",
        user_id,
        model,
        total_tokens,
        cost_usd,
    )
    return ok(True)


def _parse_monthly_hash(user_id: str, month: str, raw: Dict[str, str]) -> MonthlyUsage:
    providers: Dict = defaultdict(lambda: {"tokens": 0, "cost_usd": 0.0, "count": 0})
    models: Dict = defaultdict(lambda: {"tokens": 0, "cost_usd": 0.0, "count": 0})

    for field, value in raw.items():
        if field.startswith("provider:") and field.endswith(":tokens"):
            providers[field[9:-7]]["tokens"] = int(float(value))
        elif field.startswith("provider:") and field.endswith(":cost_usd"):
            providers[field[9:-9]]["cost_usd"] = round(float(value), 6)
        elif field.startswith("provider:") and field.endswith(":count"):
            providers[field[9:-6]]["count"] = int(float(value))
        elif field.startswith("model:") and field.endswith(":tokens"):
            models[field[6:-7]]["tokens"] = int(float(value))
        elif field.startswith("model:") and field.endswith(":cost_usd"):
            models[field[6:-9]]["cost_usd"] = round(float(value), 6)
        elif field.startswith("model:") and field.endswith(":count"):
            models[field[6:-6]]["count"] = int(float(value))

    return MonthlyUsage(
        user_id=user_id,
        month=month,
        total_tokens=int(float(raw.get("total_tokens", 0))),
        total_cost_usd=round(float(raw.get("total_cost_usd", 0.0)), 6),
        requests_count=int(float(raw.get("requests_count", 0))),
        breakdown_by_provider=dict(providers),
        breakdown_by_model=dict(models),
    )


async def get_monthly_usage(user_id: str, month: Optional[str] = None) -> MonthlyUsage:
    if month is None:
        month = _current_month()

    mon_key = f"usage:month:{user_id}:{month}"
    empty = MonthlyUsage(
        user_id=user_id,
        month=month,
        total_tokens=0,
        total_cost_usd=0.0,
        requests_count=0,
        breakdown_by_provider={},
        breakdown_by_model={},
    )

    redis = await _require_redis()
    try:
        raw = await redis.hgetall(mon_key)
        _cb.record_success()
        return _parse_monthly_hash(user_id, month, raw) if raw else empty
    except Exception as exc:
        _cb.record_failure()
        logger.warning("get_monthly_usage ERREUR : %s", type(exc).__name__)
        raise ServiceUnavailableError(_UNAVAILABLE_MSG) from exc


async def get_usage_history(user_id: str, limit: int = 50) -> List[UsageRecord]:
    idx_key = f"usage:idx:{user_id}"
    records: List[UsageRecord] = []
    redis = await _require_redis()

    try:
        members = await redis.zrevrange(idx_key, 0, limit - 1)
        for ts_str in members:
            raw = await redis.get(f"usage:rec:{user_id}:{ts_str}")
            if not raw:
                continue
            try:
                records.append(UsageRecord.model_validate_json(raw))
            except Exception:
                continue
        _cb.record_success()
    except Exception as exc:
        _cb.record_failure()
        logger.warning("get_usage_history ERREUR : %s", type(exc).__name__)
        raise ServiceUnavailableError(_UNAVAILABLE_MSG) from exc

    return records


async def get_daily_history(user_id: str, days: int = 30) -> List[dict]:
    now_ms = _now_ms()
    min_ms = now_ms - days * 24 * 3600 * 1000
    idx_key = f"usage:idx:{user_id}"
    daily: Dict[str, dict] = {}
    redis = await _require_redis()

    try:
        members = await redis.zrangebyscore(idx_key, min_ms, now_ms)
        for ts_str in members:
            raw = await redis.get(f"usage:rec:{user_id}:{ts_str}")
            if not raw:
                continue
            try:
                rec = UsageRecord.model_validate_json(raw)
            except Exception:
                continue

            day = rec.timestamp[:10]
            if day not in daily:
                daily[day] = {"date": day, "tokens": 0, "cost_usd": 0.0, "requests": 0}
            daily[day]["tokens"] += rec.input_tokens + rec.output_tokens
            daily[day]["cost_usd"] = round(daily[day]["cost_usd"] + rec.cost_usd, 6)
            daily[day]["requests"] += 1
        _cb.record_success()
    except Exception as exc:
        _cb.record_failure()
        logger.warning("get_daily_history ERREUR : %s", type(exc).__name__)
        raise ServiceUnavailableError(_UNAVAILABLE_MSG) from exc

    result = []
    for index in range(days):
        day = (datetime.now(timezone.utc) - timedelta(days=days - 1 - index)).strftime("%Y-%m-%d")
        result.append(daily.get(day, {"date": day, "tokens": 0, "cost_usd": 0.0, "requests": 0}))
    return result


async def get_org_usage(org_id: str, month: Optional[str] = None) -> dict:
    if month is None:
        month = _current_month()

    from app.core.org_manager import get_org_manager  # noqa: PLC0415

    mgr = get_org_manager()
    org = await mgr.get_org(org_id)
    if not org:
        return {
            "org_id": org_id,
            "month": month,
            "members": [],
            "total_tokens": 0,
            "total_cost_usd": 0.0,
            "requests_count": 0,
        }

    members = await mgr.get_members(org_id)
    total_tokens = 0
    total_cost = 0.0
    total_requests = 0
    members_data = []

    for member in members:
        usage = await get_monthly_usage(member.user_id, month)
        total_tokens += usage.total_tokens
        total_cost += usage.total_cost_usd
        total_requests += usage.requests_count
        members_data.append(
            {
                "user_id": member.user_id,
                "role": member.role,
                "total_tokens": usage.total_tokens,
                "total_cost_usd": round(usage.total_cost_usd, 6),
                "requests_count": usage.requests_count,
            }
        )

    return {
        "org_id": org_id,
        "month": month,
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 6),
        "requests_count": total_requests,
        "members": members_data,
    }
