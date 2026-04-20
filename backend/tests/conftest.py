"""
Fixtures pytest partagees pour tous les tests.

Mocks :
  - Redis : fakeredis.aioredis (pas de Redis reel requis)
  - LLM   : httpx_mock / responses (pas d'appels reels aux APIs)
  - Vault : utilise la fixture redis_mock automatiquement
"""
from __future__ import annotations

import asyncio
import os
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# Forcer les settings de test avant tout import app
os.environ.setdefault("SECRET_KEY", "test-secret-key-min-32-chars-long-1234567")
os.environ.setdefault("VAULT_ENCRYPTION_KEY", "dGVzdC12YXVsdC1rZXktLTMyLWJ5dGVzLW9rISEhISE=")  # 32 bytes decoded
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("LLM_PROVIDER", "anthropic")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("REDACTION_ENGINE", "spacy")


@pytest.fixture(scope="session")
def event_loop():
    """Event loop partage pour toute la session de tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def fake_redis():
    """
    Instance fakeredis.aioredis en remplacement de Redis reel.
    Utilise fakeredis si disponible, sinon MagicMock.
    """
    try:
        import fakeredis.aioredis as aioredis  # noqa: PLC0415
        r = aioredis.FakeRedis(decode_responses=False)
        yield r
        await r.aclose()
    except ImportError:
        # Fallback : mock minimal
        mock = AsyncMock()
        mock.ping = AsyncMock(return_value=True)
        mock.get = AsyncMock(return_value=None)
        mock.set = AsyncMock()
        mock.setex = AsyncMock()
        mock.delete = AsyncMock(return_value=1)
        mock.exists = AsyncMock(return_value=0)
        mock.expire = AsyncMock(return_value=1)
        mock.hset = AsyncMock()
        mock.hgetall = AsyncMock(return_value={})
        mock.hincrby = AsyncMock()
        mock.hincrbyfloat = AsyncMock()
        yield mock


@pytest_asyncio.fixture
async def vault(fake_redis):
    """Vault instance avec Redis mocke."""
    from app.core.vault import Vault  # noqa: PLC0415
    v = Vault()
    v._redis = fake_redis
    v._cb._failures = 0
    v._cb._state = "CLOSED"
    return v


@pytest.fixture
def mock_llm_response():
    """Mock d'une reponse LLM complete."""
    return "Voici ma reponse."


@pytest.fixture
def sample_user():
    return {
        "username": "testuser",
        "hashed_password": "$2b$12$placeholder",
        "role": "user",
        "org_id": "",
    }


@pytest_asyncio.fixture
async def client(fake_redis):
    """Client HTTP de test FastAPI avec Redis mocke."""
    from httpx import AsyncClient, ASGITransport  # noqa: PLC0415

    # Patcher Redis partout
    with patch("app.api.routes.auth._get_redis", return_value=fake_redis), \
         patch("app.api.routes.auth._redis_client", fake_redis), \
         patch("app.api.routes.auth._redis_ok", True):
        from app.main import app  # noqa: PLC0415
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            yield ac
