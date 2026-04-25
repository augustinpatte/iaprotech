"""
LLM gateway + streaming SSE avec ré-identification temps réel.

Deux couches :
  ① Gateway LLM (Anthropic / OpenAI)
        call_llm()           — réponse complète (str)
        stream_llm_response() — async generator de chunks texte bruts

  ② Ré-identification en streaming
        stream_reidentified() — wrap un generator de chunks LLM,
                                 accumule un buffer intelligent pour reconstruire
                                 les placeholders splittés entre chunks, puis
                                 les remplace par leurs valeurs réelles avant
                                 de les émettre en SSE JSON.

Algorithme du buffer :
  ┌──────────────────────────────────────────────────────────┐
  │ Pour chaque chunk reçu :                                  │
  │   1. Si buffer partial & chunk tardif (>timeout) → flush │
  │   2. buffer += chunk                                      │
  │   3. _split_at_partial(buffer) :                         │
  │        • rfind('[') dans buffer                          │
  │        • ']' après le '[' → pas de partial               │
  │        • suffix ~ /^\[[A-Z_]*(_\d*)?$/ → partial détecté │
  │        • sinon → '[' non-placeholder, tout est sûr       │
  │   4. yield _apply_mapping(safe_part, mapping)            │
  │   5. buffer = partial_suffix                             │
  │ Fin de stream : flush buffer restant → yield done        │
  └──────────────────────────────────────────────────────────┘

Format SSE émis par stream_reidentified :
    data: {"type": "delta", "content": "texte ré-identifié"}\n\n
    data: {"type": "done"}\n\n
    data: {"type": "error", "content": "ExceptionType"}\n\n

Logs : session_id + compteurs uniquement — zéro contenu (RGPD).
Performance cible : < 10 ms de latence ajoutée par chunk.
"""

from __future__ import annotations

import json
import re
import time
from typing import AsyncGenerator, Dict, List, Optional

import anthropic
import openai

from app.config import settings
from app.core.router import resolve_provider_model
from app.utils.logger import get_logger

logger = get_logger(__name__)

_PLACEHOLDER_KEY_FRAGMENTS = (
    "remplacer", "change_me", "changeme", "your_key", "placeholder", "xxx", "todo",
)


def _is_real_key(key: str) -> bool:
    if not key:
        return False
    low = key.lower()
    return not any(frag in low for frag in _PLACEHOLDER_KEY_FRAGMENTS)


def _provider_api_key(provider: str, provider_api_keys: Optional[Dict[str, str]] = None) -> str:
    user_key = str((provider_api_keys or {}).get(provider, "") or "").strip()
    if _is_real_key(user_key):
        return user_key
    settings_keys = {
        "anthropic": settings.ANTHROPIC_API_KEY,
        "openai": settings.OPENAI_API_KEY,
    }
    return str(settings_keys.get(provider, "") or "").strip()


# ---------------------------------------------------------------------------
# Constantes de buffer
# ---------------------------------------------------------------------------

_BUFFER_TIMEOUT: float = 2.0  # secondes avant flush forcé d'un partial

# Placeholder complet : [PERSONNE_1], [EMAIL_3], [ADRESSE_10], ...
_PLACEHOLDER_RE = re.compile(r"\[[A-Z_]+_\d+\]")

# Début potentiel d'un placeholder (suffixe de buffer) :
#   "[", "[P", "[PERS", "[PERSONNE", "[PERSONNE_", "[PERSONNE_1"
# → groupe capturant : \[ + lettres majuscules/underscore + underscore + chiffres
_PARTIAL_PREFIX_RE = re.compile(r"^\[[A-Z_]*(?:_\d*)?$")


# ---------------------------------------------------------------------------
# ① Gateway LLM — Anthropic
# ---------------------------------------------------------------------------

async def _call_anthropic(
    model: str,
    messages: List[dict],
    max_tokens: int,
    system: Optional[str],
    api_key: str,
) -> dict:
    resolved_model = resolve_provider_model("anthropic", model)
    client = anthropic.AsyncAnthropic(api_key=api_key)
    kwargs: dict = dict(model=resolved_model, max_tokens=max_tokens, messages=messages)
    if system:
        kwargs["system"] = system
    try:
        response = await client.messages.create(**kwargs)
        usage = getattr(response, "usage", None)
        return {
            "text": response.content[0].text,
            "usage": {
                "prompt_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                "completion_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            } if usage else None,
        }
    except Exception as exc:
        logger.error(
            "anthropic.call_error | model=%s | resolved_model=%s | exc=%s",
            model,
            resolved_model,
            type(exc).__name__,
        )
        raise


async def _stream_anthropic(
    model: str,
    messages: List[dict],
    max_tokens: int,
    system: Optional[str],
    api_key: str,
) -> AsyncGenerator[str, None]:
    resolved_model = resolve_provider_model("anthropic", model)
    client = anthropic.AsyncAnthropic(api_key=api_key)
    kwargs: dict = dict(model=resolved_model, max_tokens=max_tokens, messages=messages)
    if system:
        kwargs["system"] = system
    try:
        async with client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield text
    except Exception as exc:
        logger.error(
            "anthropic.stream_error | model=%s | exc=%s",
            model,
            type(exc).__name__,
        )
        raise


# ---------------------------------------------------------------------------
# ① Gateway LLM — OpenAI
# ---------------------------------------------------------------------------

async def _call_openai(
    model: str,
    messages: List[dict],
    max_tokens: int,
    system: Optional[str],
    api_key: str,
) -> dict:
    client = openai.AsyncOpenAI(api_key=api_key)
    msgs = ([{"role": "system", "content": system}] + messages) if system else messages
    response = await client.chat.completions.create(
        model=model,
        messages=msgs,
        max_tokens=max_tokens,
    )
    usage = getattr(response, "usage", None)
    return {
        "text": response.choices[0].message.content,
        "usage": {
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        } if usage else None,
    }


async def _stream_openai(
    model: str,
    messages: List[dict],
    max_tokens: int,
    system: Optional[str],
    api_key: str,
) -> AsyncGenerator[str, None]:
    client = openai.AsyncOpenAI(api_key=api_key)
    msgs = ([{"role": "system", "content": system}] + messages) if system else messages
    stream = await client.chat.completions.create(
        model=model,
        messages=msgs,
        max_tokens=max_tokens,
        stream=True,
    )
    async for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta


# ---------------------------------------------------------------------------
# ① Interface publique gateway
# ---------------------------------------------------------------------------

async def call_llm(
    provider: str,
    model: str,
    messages: List[dict],
    max_tokens: int = 2048,
    system: Optional[str] = None,
    provider_api_keys: Optional[Dict[str, str]] = None,
) -> dict:
    """Retourne la réponse LLM complète et, si disponible, l'usage provider."""
    logger.debug(
        "call_llm | provider=%s | model=%s | messages=%d",
        provider, model, len(messages),
    )
    if provider == "anthropic":
        return await _call_anthropic(
            model,
            messages,
            max_tokens,
            system,
            _provider_api_key(provider, provider_api_keys),
        )
    if provider == "openai":
        return await _call_openai(
            model,
            messages,
            max_tokens,
            system,
            _provider_api_key(provider, provider_api_keys),
        )
    raise ValueError(f"Provider inconnu : {provider!r}")


async def stream_llm_response(
    provider: str,
    model: str,
    messages: List[dict],
    max_tokens: int = 2048,
    system: Optional[str] = None,
    provider_api_keys: Optional[Dict[str, str]] = None,
) -> AsyncGenerator[str, None]:
    """Async generator — yield les chunks texte bruts du LLM."""
    logger.debug(
        "stream_llm | provider=%s | model=%s | messages=%d",
        provider, model, len(messages),
    )
    if provider == "anthropic":
        async for chunk in _stream_anthropic(
            model,
            messages,
            max_tokens,
            system,
            _provider_api_key(provider, provider_api_keys),
        ):
            yield chunk
    elif provider == "openai":
        async for chunk in _stream_openai(
            model,
            messages,
            max_tokens,
            system,
            _provider_api_key(provider, provider_api_keys),
        ):
            yield chunk
    else:
        raise ValueError(f"Provider inconnu : {provider!r}")


# ---------------------------------------------------------------------------
# ② Buffer helpers — ré-identification temps réel
# ---------------------------------------------------------------------------

def _split_at_partial(buffer: str) -> tuple[str, str]:
    """
    Divise le buffer en (safe_part, partial_suffix).

    safe_part      : peut être yielded immédiatement (tous les placeholders
                     complets à l'intérieur seront remplacés par _apply_mapping)
    partial_suffix : début potentiel de placeholder en fin de buffer
                     (ex: "[PERS", "[EMAIL_", "[") → à accumuler

    Règles :
      • Pas de '[' dans buffer                → (buffer, "")
      • ']' apparaît après le dernier '['     → (buffer, "")  ← placeholder fermé
      • Suffix ~ /^\[[A-Z_]*(_\d*)?$/        → (buffer[:idx], suffix)
      • '[' présent mais pas de pattern valid → (buffer, "")  ← pas un placeholder

    Exemples :
      "Bonjour [PERSONNE_1] !"       → ("Bonjour [PERSONNE_1] !", "")
      "Bonjour [PERS"                → ("Bonjour ", "[PERS")
      "ok [PERSONNE_1] et [EMAIL"    → ("ok [PERSONNE_1] et ", "[EMAIL")
      "prix : [10 euros"             → ("prix : [10 euros", "")   ← pas majuscule
      "text ["                       → ("text ", "[")
      "text [PERSONNE_"              → ("text ", "[PERSONNE_")
      "text [PERSONNE_12"            → ("text ", "[PERSONNE_12")
    """
    idx = buffer.rfind("[")

    if idx == -1:
        # Aucun crochet — tout est sûr
        return buffer, ""

    suffix = buffer[idx:]

    # Si ']' apparaît après ce '[', le crochet est fermé → pas de partial
    if "]" in suffix:
        return buffer, ""

    # Le suffix correspond-il au début d'un placeholder valide ?
    if _PARTIAL_PREFIX_RE.match(suffix):
        return buffer[:idx], suffix

    # '[' présent mais ne ressemble pas à un placeholder (ex: "[42", "[lower")
    return buffer, ""


def _apply_mapping(text: str, mapping: dict[str, str]) -> str:
    """
    Remplace tous les placeholders complets dans *text* par leurs valeurs originales.
    Inoffensif si mapping est vide ou text est vide.
    """
    if not text or not mapping:
        return text
    return _PLACEHOLDER_RE.sub(
        lambda m: mapping.get(m.group(0), m.group(0)),
        text,
    )


def _sse(data: dict) -> str:
    """Formate un dict en événement SSE sur une ligne."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# ② Interface publique — streaming avec ré-identification
# ---------------------------------------------------------------------------

async def stream_reidentified(
    chunks: AsyncGenerator[str, None],
    vault,                         # Vault — import tardif pour éviter cycle
    session_id: str,
    buffer_timeout: float = _BUFFER_TIMEOUT,
    user_id: str = "",
) -> AsyncGenerator[str, None]:
    """
    Wrap un async generator de chunks texte LLM avec ré-identification temps réel.

    Stratégie buffer :
      • Chaque chunk est ajouté au buffer
      • _split_at_partial() détecte si le buffer se termine par un début de
        placeholder splitté entre deux chunks (ex: "[PERS" + "ONNE_1]")
      • La partie sûre est immédiatement remplacée et émise
      • Le partial est conservé jusqu'au chunk suivant
      • Si aucun chunk n'arrive pendant buffer_timeout secondes, le partial
        est flushé tel quel (avec ses tokens bruts) pour ne pas bloquer le client

    Yield :
        "data: {\"event\": \"delta\", \"data\": \"texte\"}\n\n"
        "data: {\"event\": \"done\"}\n\n"
        "data: {\"event\": \"error\", \"message\": \"ExceptionType\"}\n\n"

    Args:
        chunks         : async generator de str (sortie de stream_llm_response)
        vault          : instance Vault pour get_mapping()
        session_id     : identifiant de session vault
        buffer_timeout : secondes d'attente max avant flush forcé du partial

    Logs zero-content : session_id + compteurs uniquement.
    Performance : regex O(n) sur chaque chunk — latence ajoutée < 10 ms.
    """
    # Chargement unique du mapping — évite un appel Redis par chunk
    mapping: dict[str, str] = await vault.get_mapping(session_id, user_id=user_id) or {}

    buffer: str = ""
    chunks_yielded: int = 0
    timeout_flushes: int = 0
    last_chunk_at: float = time.monotonic()

    try:
        async for chunk in chunks:
            now = time.monotonic()

            # ── Timeout check : flush le partial si trop vieux ────────────────
            if buffer and (now - last_chunk_at) > buffer_timeout:
                timeout_flushes += 1
                logger.debug(
                    "stream.timeout_flush | session=%s | elapsed=%.2fs",
                    session_id, now - last_chunk_at,
                )
                # Flush brut — le partial peut contenir un token incomplet
                yield _sse({"type": "delta", "content": buffer})
                buffer = ""

            last_chunk_at = now
            buffer += chunk

            # ── Détection du partial en fin de buffer ─────────────────────────
            safe_part, partial_suffix = _split_at_partial(buffer)

            if safe_part:
                restored = _apply_mapping(safe_part, mapping)
                if restored:
                    chunks_yielded += 1
                    yield _sse({"type": "delta", "content": restored})

            buffer = partial_suffix

    except Exception as exc:
        # Erreur LLM ou réseau — on émet l'error SSE et on sort proprement
        logger.error(
            "stream.error | session=%s | %s",
            session_id, type(exc).__name__,
        )
        # Flush le buffer courant s'il en reste
        if buffer:
            restored = _apply_mapping(buffer, mapping)
            if restored:
                yield _sse({"type": "delta", "content": restored})
        yield _sse({"type": "error", "content": f"{type(exc).__name__}: {exc}"})
        return

    # ── Flush post-stream : vide le buffer restant ────────────────────────────
    if buffer:
        restored = _apply_mapping(buffer, mapping)
        if restored:
            chunks_yielded += 1
            yield _sse({"type": "delta", "content": restored})

    logger.debug(
        "stream.done | session=%s | chunks_out=%d | timeout_flushes=%d",
        session_id, chunks_yielded, timeout_flushes,
    )
    yield _sse({"type": "done"})
