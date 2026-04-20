"""
Tests du ChatService — pseudonymisation, re-identification, isolation.

Couvre :
  - pseudonymize_before_sending : le LLM ne voit pas les PII
  - reidentify_response : la reponse est restauree
  - isolation_between_projects : deux sessions distinctes
  - unknown_model -> erreur claire
"""
from __future__ import annotations

from typing import AsyncGenerator, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from app.core.interfaces import VaultInterface, RedactorInterface, LLMRouterInterface
from app.core.result import Ok, Err, ok, err


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------

class MockVault(VaultInterface):
    def __init__(self):
        self._store: Dict[str, Dict[str, str]] = {}

    async def store(self, user_id: str, session_id: str, mapping: Dict[str, str]) -> Ok:
        key = f"{user_id}:{session_id}"
        self._store.setdefault(key, {}).update(mapping)
        return ok(True)

    async def get(self, user_id: str, session_id: str) -> Ok:
        key = f"{user_id}:{session_id}"
        return ok(self._store.get(key, None))

    async def delete(self, user_id: str, session_id: str) -> Ok:
        key = f"{user_id}:{session_id}"
        existed = key in self._store
        self._store.pop(key, None)
        return ok(existed)

    async def restore(self, user_id: str, session_id: str, text: str) -> str:
        key = f"{user_id}:{session_id}"
        mapping = self._store.get(key, {})
        import re
        for token, original in mapping.items():
            text = re.sub(re.escape(token), original, text, flags=re.IGNORECASE)
        return text


class MockRedactor(RedactorInterface):
    """Redactor qui remplace 'Jean Dupont' par [PERSONNE_1]."""

    async def pseudonymize(self, text: str, language: str = "auto", entities=None) -> Ok:
        mapping = {}
        pseudonymized = text
        if "Jean Dupont" in text:
            pseudonymized = text.replace("Jean Dupont", "[PERSONNE_1]")
            mapping["[PERSONNE_1]"] = "Jean Dupont"
        return ok((pseudonymized, mapping))

    async def reidentify(self, text: str, mapping: Dict[str, str]) -> Ok:
        import re
        for token, original in mapping.items():
            text = re.sub(re.escape(token), original, text, flags=re.IGNORECASE)
        return ok(text)


class MockRouter(LLMRouterInterface):
    """Router qui renvoie une reponse fixe incluant les tokens."""

    def __init__(self, response: str = "Bonjour [PERSONNE_1], comment puis-je aider ?"):
        self._response = response
        self.last_messages = []

    async def call(self, messages: list, model: str, max_tokens: int = 2048, system=None) -> Ok:
        self.last_messages = messages
        return ok(self._response)

    async def stream(self, messages: list, model: str, max_tokens: int = 2048, system=None):
        self.last_messages = messages
        for chunk in self._response.split():
            yield chunk + " "


class ErrorRouter(LLMRouterInterface):
    async def call(self, messages, model, max_tokens=2048, system=None) -> Err:
        return err(f"Modele inconnu: {model!r}", "LLM_UNKNOWN_MODEL")

    async def stream(self, messages, model, max_tokens=2048, system=None):
        raise ValueError(f"Modele inconnu: {model!r}")
        return
        yield  # noqa: unreachable


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_pseudonymizes_before_sending():
    """Le LLM ne doit jamais voir les PII — seuls les tokens doivent etre envoyes."""
    from app.services.chat_service import ChatService  # noqa: PLC0415

    vault = MockVault()
    redactor = MockRedactor()
    router = MockRouter()
    service = ChatService(vault=vault, redactor=redactor, router=router)

    messages = [{"role": "user", "content": "Bonjour, je suis Jean Dupont."}]
    result = await service.call_response(
        user_id="alice",
        session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        messages=messages,
        model="claude-sonnet",
    )

    # Verifier que le LLM a recu les messages pseudonymises
    assert router.last_messages is not None
    llm_content = router.last_messages[-1]["content"]
    assert "Jean Dupont" not in llm_content
    assert "[PERSONNE_1]" in llm_content

    # Verifier que la reponse est bien re-identifiee
    assert result.ok
    assert "Jean Dupont" in result.value
    assert "[PERSONNE_1]" not in result.value


@pytest.mark.asyncio
async def test_chat_reidentifies_response():
    """La reponse LLM avec tokens doit etre restauree avec les vraies valeurs."""
    from app.services.chat_service import ChatService  # noqa: PLC0415

    vault = MockVault()
    redactor = MockRedactor()
    router = MockRouter("Merci [PERSONNE_1] pour votre message.")
    service = ChatService(vault=vault, redactor=redactor, router=router)

    result = await service.call_response(
        user_id="bob",
        session_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        messages=[{"role": "user", "content": "Je m'appelle Jean Dupont."}],
        model="gpt-4",
    )

    assert result.ok
    assert "Jean Dupont" in result.value
    assert "[PERSONNE_1]" not in result.value


@pytest.mark.asyncio
async def test_chat_isolation_between_projects():
    """Deux sessions/projets differents ont des mappings isoles."""
    from app.services.chat_service import ChatService  # noqa: PLC0415

    vault = MockVault()
    redactor = MockRedactor()
    service = ChatService(vault=vault, redactor=redactor, router=MockRouter())

    # Session 1 : Jean Dupont
    await service.call_response(
        user_id="alice",
        session_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        messages=[{"role": "user", "content": "Je suis Jean Dupont."}],
        model="claude-sonnet",
    )

    # Session 2 : different user_id + session_id -> mapping distinct
    mapping_session2 = await vault.get("alice", "dddddddd-dddd-4ddd-8ddd-dddddddddddd")
    assert mapping_session2.value is None  # Pas de cross-contamination


@pytest.mark.asyncio
async def test_chat_unknown_model_returns_clear_error():
    """Un modele inconnu doit retourner une erreur claire, pas une exception non geree."""
    from app.services.chat_service import ChatService  # noqa: PLC0415

    vault = MockVault()
    redactor = MockRedactor()
    router = ErrorRouter()
    service = ChatService(vault=vault, redactor=redactor, router=router)

    result = await service.call_response(
        user_id="charlie",
        session_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        messages=[{"role": "user", "content": "Test."}],
        model="unknown-model-xyz",
    )

    assert not result.ok
    assert result.code in ("LLM_UNKNOWN_MODEL", "LLM_ERROR")


@pytest.mark.asyncio
async def test_chat_stream_yields_sse_events():
    """stream_response doit yielder des evenements SSE valides."""
    import json as _json
    from app.services.chat_service import ChatService  # noqa: PLC0415

    vault = MockVault()
    redactor = MockRedactor()
    router = MockRouter("Bonjour Jean Dupont !")
    service = ChatService(vault=vault, redactor=redactor, router=router)

    events = []
    async for sse_line in service.stream_response(
        user_id="dana",
        session_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
        messages=[{"role": "user", "content": "Salut Jean Dupont."}],
        model="claude-sonnet",
    ):
        if sse_line.startswith("data: "):
            events.append(_json.loads(sse_line[6:].strip()))

    types = [e["type"] for e in events]
    assert "start" in types
    assert "done" in types
    assert "error" not in types
