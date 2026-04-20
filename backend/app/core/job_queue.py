import json
import uuid
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis

from app.config import settings

_redis_client: Optional[aioredis.Redis] = None
_job_queue: Optional["JobQueue"] = None


class JobQueue:
    def __init__(self, redis_client: aioredis.Redis):
        self.redis = redis_client
        self.QUEUE_KEY = "jobs:pending"
        self.RESULT_PREFIX = "jobs:result:"
        self.TTL = 3600

    async def enqueue(self, job_type: str, payload: dict) -> str:
        job_id = str(uuid.uuid4())
        job = {
            "job_id": job_id,
            "type": job_type,
            "payload": payload,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
        }
        await self.redis.lpush(self.QUEUE_KEY, json.dumps(job))
        await self.redis.setex(
            f"{self.RESULT_PREFIX}{job_id}",
            self.TTL,
            json.dumps(
                {
                    "job_id": job_id,
                    "status": "pending",
                    "created_at": job["created_at"],
                    "user_id": payload.get("user_id"),
                }
            ),
        )
        return job_id

    async def dequeue(self, timeout: int = 5) -> Optional[dict]:
        item = await self.redis.brpop(self.QUEUE_KEY, timeout=timeout)
        if not item:
            return None
        _, raw_job = item
        return json.loads(raw_job)

    async def get_result(self, job_id: str) -> dict | None:
        data = await self.redis.get(f"{self.RESULT_PREFIX}{job_id}")
        return json.loads(data) if data else None

    async def set_result(self, job_id: str, result: dict) -> None:
        await self.redis.setex(
            f"{self.RESULT_PREFIX}{job_id}",
            self.TTL,
            json.dumps(result),
        )

    async def set_status(self, job_id: str, status: str, **extra) -> None:
        current = await self.get_result(job_id) or {"job_id": job_id}
        current["status"] = status
        current.update(extra)
        await self.set_result(job_id, current)


async def get_job_queue() -> JobQueue:
    global _redis_client, _job_queue
    if _job_queue is not None:
        return _job_queue
    if _redis_client is None:
        _redis_client = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=10,
        )
    _job_queue = JobQueue(_redis_client)
    return _job_queue
