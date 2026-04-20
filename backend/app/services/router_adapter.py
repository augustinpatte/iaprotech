"""
LiteLLMRouterAdapter — adapte call_llm/stream_llm vers LLMRouterInterface.
"""
from __future__ import annotations

from typing import AsyncGenerator, Optional, Tuple

from app.core.interfaces import LLMRouterInterface
from app.core.result import Result, ok, err
from app.utils.logger import get_logger

logger = get_logger(__name__)


def normalize_llm_exception(exc: Exception, provider: Optional[str], model: str) -> Tuple[str, str]:
    raw = str(exc or "").strip()
    lower = raw.lower()
    provider_name = provider or "llm"

    if "invalid x-api-key" in lower or "authentication_error" in lower:
        if provider_name == "anthropic":
            return (
                "La cle API Anthropic est invalide ou expiree. Verifiez ANTHROPIC_API_KEY dans .env puis redemarrez le backend.",
                "PROVIDER_AUTH_ERROR",
            )
        if provider_name == "openai":
            return (
                "La cle API OpenAI est invalide ou expiree. Verifiez OPENAI_API_KEY dans .env.",
                "PROVIDER_AUTH_ERROR",
            )
        if provider_name == "google":
            return (
                "La cle API Gemini est invalide ou non autorisee pour ce service. Verifiez GEMINI_API_KEY dans .env.",
                "PROVIDER_AUTH_ERROR",
            )

    if "insufficient_quota" in lower or "quota" in lower or "billing" in lower:
        return (
            f"Le compte {provider_name} n'a pas de quota disponible ou le billing n'est pas actif.",
            "PROVIDER_QUOTA_ERROR",
        )

    if provider_name == "google" and ("gemini" in lower or "google" in lower or "api key not valid" in lower):
        return (
            f"Gemini a refuse la requete pour le modele {model}. Verifiez la cle Google et le modele configure.",
            "PROVIDER_CONFIG_ERROR",
        )

    if "not_found_error" in lower or ("model:" in lower and "not found" in lower):
        return (
            f"Le modele {model} n'est plus disponible chez {provider_name}. Selectionnez un autre modele ou repassez en mode Auto.",
            "MODEL_NOT_FOUND",
        )

    return (
        f"Le fournisseur {provider_name} a refuse la requete. Reessayez ou verifiez sa configuration.",
        "LLM_ERROR",
    )


def _resolve_litellm_model_id(model: str) -> str:
    from app.core.router import MODEL_REGISTRY  # noqa: PLC0415

    meta = MODEL_REGISTRY.get(model)
    if meta and meta.get("litellm_model_id"):
        return meta["litellm_model_id"]
    return model


class LiteLLMRouterAdapter(LLMRouterInterface):
    """
    Adapte les fonctions core (call_llm_via_litellm, stream_llm_via_litellm)
    vers LLMRouterInterface.
    """

    async def call(
        self,
        messages: list,
        model: str,
        max_tokens: int = 2048,
        system: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> Result:
        try:
            if provider in {"anthropic", "openai"}:
                from app.core.streaming import call_llm  # noqa: PLC0415

                response = await call_llm(
                    provider=provider,
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    system=system,
                )
            else:
                from app.core.router import call_llm_via_litellm  # noqa: PLC0415

                response = await call_llm_via_litellm(
                    litellm_model_id=_resolve_litellm_model_id(model),
                    messages=messages,
                    max_tokens=max_tokens,
                    system=system,
                )
            return ok(response)
        except Exception as exc:
            message, code = normalize_llm_exception(exc, provider, model)
            logger.error(
                "router.call ERREUR | provider=%s | model=%s | %s | raw=%s",
                provider or "litellm",
                model,
                type(exc).__name__,
                raw if (raw := str(exc)) else type(exc).__name__,
                exc_info=True,
            )
            return err(message, code)

    async def stream(
        self,
        messages: list,
        model: str,
        max_tokens: int = 2048,
        system: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        try:
            if provider in {"anthropic", "openai"}:
                from app.core.streaming import stream_llm_response  # noqa: PLC0415

                async for chunk in stream_llm_response(
                    provider=provider,
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    system=system,
                ):
                    yield chunk
                return

            from app.core.router import stream_llm_via_litellm  # noqa: PLC0415

            async for chunk in stream_llm_via_litellm(
                litellm_model_id=_resolve_litellm_model_id(model),
                messages=messages,
                max_tokens=max_tokens,
                system=system,
            ):
                yield chunk
        except Exception as exc:
            message, _ = normalize_llm_exception(exc, provider, model)
            logger.error(
                "router.stream ERREUR | provider=%s | model=%s | %s | raw=%s",
                provider or "litellm",
                model,
                type(exc).__name__,
                str(exc),
                exc_info=True,
            )
            raise RuntimeError(message) from exc
