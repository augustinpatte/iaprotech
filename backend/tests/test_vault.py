"""
Tests du Vault — chiffrement AES-256-GCM + circuit breaker.

Couvre :
  - store + get avec user_id (isolation)
  - get avec mauvais user_id -> None (isolation garantie)
  - delete -> supprime les donnees
  - circuit breaker -> OPEN apres N echecs Redis
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio


@pytest.mark.asyncio
async def test_store_and_get_with_user_id(vault):
    """store_mapping + get_mapping avec user_id retourne le meme mapping."""
    mapping = {"[PERSONNE_1]": "Jean Dupont", "[EMAIL_1]": "j@example.com"}
    session_id = "11111111-1111-4111-8111-111111111111"
    user_id = "alice"

    ok = await vault.store_mapping(session_id, mapping, user_id=user_id)
    assert ok is True

    retrieved = await vault.get_mapping(session_id, user_id=user_id)
    assert retrieved is not None
    assert retrieved["[PERSONNE_1]"] == "Jean Dupont"
    assert retrieved["[EMAIL_1]"] == "j@example.com"


@pytest.mark.asyncio
async def test_get_wrong_user_id(vault):
    """get_mapping avec mauvais user_id ne retourne pas les donnees d'un autre user."""
    mapping = {"[PERSONNE_1]": "Secret"}
    session_id = "22222222-2222-4222-8222-222222222222"

    await vault.store_mapping(session_id, mapping, user_id="alice")

    # Bob ne doit pas voir les donnees d'Alice
    retrieved = await vault.get_mapping(session_id, user_id="bob")
    assert retrieved is None


@pytest.mark.asyncio
async def test_delete_removes_data(vault):
    """delete_mapping supprime les donnees et retourne True si existait."""
    mapping = {"[PHONE_1]": "+33612345678"}
    session_id = "33333333-3333-4333-8333-333333333333"
    user_id = "charlie"

    await vault.store_mapping(session_id, mapping, user_id=user_id)
    existed = await vault.delete_mapping(session_id, user_id=user_id)
    assert existed is True

    # Apres delete, get doit retourner None
    retrieved = await vault.get_mapping(session_id, user_id=user_id)
    assert retrieved is None


@pytest.mark.asyncio
async def test_delete_nonexistent_returns_false(vault):
    """delete_mapping sur session inexistante retourne False."""
    existed = await vault.delete_mapping(
        "99999999-9999-4999-8999-999999999999",
        user_id="nobody",
    )
    assert existed is False


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_failures():
    """Apres FAILURE_THRESHOLD echecs Redis, le circuit passe OPEN."""
    from app.core.vault import Vault, _CircuitBreaker  # noqa: PLC0415

    v = Vault()
    cb = v._cb

    # Simuler N echecs consecutifs
    for _ in range(cb.FAILURE_THRESHOLD):
        cb.record_failure()

    assert cb.state == "OPEN"
    assert cb.is_open is True


@pytest.mark.asyncio
async def test_circuit_breaker_recovers():
    """Apres un succes, le circuit repasse CLOSED."""
    from app.core.vault import _CircuitBreaker  # noqa: PLC0415

    cb = _CircuitBreaker()
    for _ in range(cb.FAILURE_THRESHOLD):
        cb.record_failure()
    assert cb.state == "OPEN"

    cb.record_success()
    assert cb.state == "CLOSED"
    assert cb.is_open is False


@pytest.mark.asyncio
async def test_vault_result_interface(vault):
    """API Result : store/get/delete retournent Ok/Err."""
    from app.core.result import Ok, Err  # noqa: PLC0415

    mapping = {"[PERSONNE_1]": "Test"}
    session_id = "44444444-4444-4444-8444-444444444444"
    user_id = "dave"

    store_result = await vault.store(user_id, session_id, mapping)
    assert isinstance(store_result, Ok)
    assert store_result.ok is True

    get_result = await vault.get(user_id, session_id)
    assert isinstance(get_result, Ok)
    assert get_result.value is not None

    delete_result = await vault.delete(user_id, session_id)
    assert isinstance(delete_result, Ok)
    assert delete_result.value is True
