"""
ChatService -- orchestre vault + redactor + router pour le chat LLM.

Responsabilites :
  1. Pseudonymiser les messages utilisateur via RedactorInterface
  2. Stocker le mapping dans VaultInterface
  3. Appeler / streamer le LLM via LLMRouterInterface
  4. Re-identifier la reponse complete ou les chunks streames
  5. Porter le flux SSE complet pour que la route reste declarative
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator, Dict, List, Optional

from fastapi import HTTPException, status

from app.config import settings
from app.core.audit_trail import AuditEvent, get_audit_trail
from app.core.interfaces import LLMRouterInterface, RedactorInterface, VaultInterface
from app.core.policy_engine import ProtectionMode, get_effective_policy
from app.core.result import Result, err, ok
from app.core.router import (
    MODEL_REGISTRY,
    ModelSelection,
    analyze_request,
    estimate_cost,
    estimate_tokens,
    select_model,
)
from app.core.usage_tracker import get_monthly_usage, track_usage
from app.utils.logger import get_logger

logger = get_logger(__name__)

_PLACEHOLDER_RE = re.compile(r"\[[A-Z_]+_\d+\]")
_PARTIAL_PREFIX_RE = re.compile(r"^\[[A-Z_]*(?:_\d*)?$")
_BUFFER_TIMEOUT = 2.0
_VALID_PROVIDERS = frozenset({"anthropic", "openai", "google", "mistral"})


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _split_at_partial(buffer: str) -> tuple[str, str]:
    idx = buffer.rfind("[")
    if idx == -1:
        return buffer, ""
    suffix = buffer[idx:]
    if "]" in suffix:
        return buffer, ""
    if _PARTIAL_PREFIX_RE.match(suffix):
        return buffer[:idx], suffix
    return buffer, ""


def _apply_mapping(text: str, mapping: Dict[str, str]) -> str:
    if not text or not mapping:
        return text
    return _PLACEHOLDER_RE.sub(lambda m: mapping.get(m.group(0), m.group(0)), text)


def _build_restored_user_message(content: str, attachment_name: Optional[str]) -> str:
    text = str(content or "")
    if not attachment_name:
        return text
    safe_name = attachment_name.strip()
    if not safe_name:
        return text
    if text:
        return f"[Document joint: {safe_name}]\n\n{text}"
    return f"[Document joint: {safe_name}]"


class ChatService:
    """
    Orchestre la pseudonymisation -> LLM -> re-identification.

    Depend des interfaces abstraites pour les composants critiques (vault,
    redactor, router) et charge le reste du contexte metier a la demande.
    """

    def __init__(
        self,
        vault: VaultInterface,
        redactor: RedactorInterface,
        router: LLMRouterInterface,
    ) -> None:
        self.vault = vault
        self.redactor = redactor
        self.router = router

    async def pseudonymize_messages(
        self,
        messages: List[dict],
        user_id: str,
        session_id: str,
        entities: Optional[List[str]] = None,
        score_threshold: float = 0.6,
        excluded_entity_types: Optional[List[str]] = None,
    ) -> tuple[List[dict], int]:
        sanitised: List[dict] = []
        total = 0
        stored_any_mapping = False

        for msg in messages:
            if msg.get("role") not in ("user", "system"):
                sanitised.append(msg)
                continue

            result = await self.redactor.pseudonymize(
                msg["content"],
                entities=entities,
                score_threshold=score_threshold,
                excluded_entity_types=excluded_entity_types,
            )
            if not result.ok:
                logger.error(
                    "chat_service.pseudonymize ERREUR | user=%s | %s",
                    user_id,
                    result.message,
                )
                sanitised.append(msg)
                continue

            pseudonymized, mapping = result.value
            if mapping:
                store_result = await self.vault.store(user_id, session_id, mapping)
                if not store_result.ok:
                    if store_result.code == "VAULT_UNAVAILABLE":
                        raise RuntimeError(store_result.message)
                    logger.warning("chat_service.vault_store WARN | %s", store_result.message)
                total += len(mapping)
                stored_any_mapping = True
            sanitised.append({"role": msg["role"], "content": pseudonymized})

        if not stored_any_mapping:
            store_result = await self.vault.store(user_id, session_id, {})
            if not store_result.ok:
                if store_result.code == "VAULT_UNAVAILABLE":
                    raise RuntimeError(store_result.message)
                logger.warning("chat_service.vault_store_empty WARN | %s", store_result.message)

        return sanitised, total

    async def call_response(
        self,
        user_id: str,
        session_id: str,
        messages: List[dict],
        model: str,
        max_tokens: int = 2048,
        system: Optional[str] = None,
        entities: Optional[List[str]] = None,
        provider: Optional[str] = None,
    ) -> Result:
        try:
            sanitised, _ = await self.pseudonymize_messages(messages, user_id, session_id, entities)
        except RuntimeError as exc:
            return err(str(exc), "VAULT_UNAVAILABLE")

        call_result = await self.router.call(
            sanitised,
            model,
            max_tokens=max_tokens,
            system=system,
            provider=provider,
        )
        if not call_result.ok:
            return call_result

        response_payload = call_result.value
        raw_text = response_payload.get("text", "") if isinstance(response_payload, dict) else str(response_payload or "")
        restored = await self.vault.restore(session_id, raw_text, user_id=user_id)
        return ok(restored)

    async def stream_response(
        self,
        *,
        user_id: str,
        org_id: str,
        messages: List[dict],
        project_id: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        max_tokens: int = 2048,
        system: Optional[str] = None,
        preferred_model: Optional[str] = None,
        protection_mode: str = ProtectionMode.PRIVACY.value,
        min_confidence_score: Optional[float] = None,
        attachment_name: Optional[str] = None,
        attachment_types: Optional[List[str]] = None,
    ) -> AsyncGenerator[str, None]:
        async def _event_generator() -> AsyncGenerator[str, None]:
            project = None
            project_manager = None
            model_used = model or ""
            provider_name = (provider or settings.LLM_PROVIDER).lower()
            policy = get_effective_policy(protection_mode, None)
            total_entities = 0
            tracked_cost = 0.0

            async def _log_stream_event(success: bool, error_code: Optional[str] = None) -> None:
                try:
                    await get_audit_trail().log(
                        AuditEvent.build(
                            user_id=user_id,
                            org_id=org_id,
                            project_id=project.project_id if project else project_id,
                            action="chat",
                            model_used=model_used,
                            protection_mode=policy.mode.value,
                            entities_masked=total_entities,
                            file_type="chat",
                            cost_usd=tracked_cost if success else 0.0,
                            success=success,
                            error_code=error_code,
                        )
                    )
                except Exception as exc:
                    logger.warning("chat_service.audit WARN | %s", type(exc).__name__)

            try:
                yield _sse({"type": "status", "step": "preparing"})

                async with asyncio.timeout(60):
                    provider_name, model_display, model_used, selection = await self._resolve_model(
                        messages=messages,
                        provider=provider,
                        model=model,
                        preferred_model=preferred_model,
                        attachment_types=attachment_types,
                    )
                    yield _sse({"type": "status", "step": "policy"})
                    org_settings = await self._check_org_policies(
                        user_id=user_id,
                        org_id=org_id,
                        provider=provider_name,
                    )
                    org_policy = getattr(org_settings, "policy", None)
                    minimum_mode = getattr(org_policy, "min_protection_mode", "privacy")
                    policy = get_effective_policy(protection_mode, minimum_mode)
                    blocked_models = set(getattr(org_policy, "forbidden_models", []))
                    for candidate in (model, preferred_model, model_display, model_used):
                        if candidate and candidate in blocked_models:
                            raise HTTPException(
                                status_code=status.HTTP_403_FORBIDDEN,
                                detail=f"Modele {candidate!r} interdit par la politique de l'organisation.",
                            )
                    score_threshold = min_confidence_score if min_confidence_score is not None else policy.min_confidence_score
                    redaction_entities = org_settings.redaction_entities or list(policy.active_entities)

                    project_manager = self._get_project_manager()
                    project = await project_manager.get_project(project_id, user_id) if project_id else None
                    yield _sse({"type": "status", "step": "history"})

                    if project:
                        session_id = project.session_id
                        if project.messages:
                            mapping_result = await self.vault.get(user_id, session_id)
                            if not mapping_result.ok:
                                raise HTTPException(
                                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                    detail="Service temporairement indisponible",
                                )
                            if mapping_result.value is None:
                                raise HTTPException(
                                    status_code=status.HTTP_410_GONE,
                                    detail=(
                                        "Session expiree -- les donnees ont ete supprimees conformement "
                                        "a la politique RGPD. Demarrez une nouvelle conversation."
                                    ),
                                )
                    else:
                        session_id = str(uuid.uuid4())

                    if project:
                        last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
                        messages_to_pseudonymize = [last_user] if last_user else messages
                    else:
                        messages_to_pseudonymize = messages

                    try:
                        yield _sse({"type": "status", "step": "redacting"})
                        new_pseudonymised, total_entities = await self.pseudonymize_messages(
                            messages_to_pseudonymize,
                            user_id,
                            session_id,
                            redaction_entities,
                            score_threshold=score_threshold,
                            excluded_entity_types=list(policy.excluded_entity_types),
                        )
                    except RuntimeError as exc:
                        raise HTTPException(
                            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=str(exc),
                        ) from exc

                    if project and project.messages:
                        history_redacted = [
                            {"role": message.role, "content": message.content_redacted}
                            for message in project.messages
                        ]
                        sanitised_messages = history_redacted + new_pseudonymised
                    else:
                        sanitised_messages = new_pseudonymised

                    cost = estimate_cost(model_display, sum(len(m.get("content", "")) for m in messages), max_tokens) if selection else None
                    session_expires_at = (
                        datetime.now(timezone.utc) + timedelta(seconds=settings.REDIS_TTL_SECONDS)
                    ).isoformat()

                    if not project:
                        first_content = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
                        if first_content:
                            project = await project_manager.create_project(
                                user_id,
                                first_content,
                                session_id=session_id,
                            )

                    yield _sse({"type": "status", "step": "provider", "model": model_display})
                    yield _sse(
                        {
                            "type": "start",
                            "session_id": session_id,
                            "project_id": project.project_id if project else None,
                            "redacted_entities": total_entities,
                            "model_used": model_used,
                            "protection_mode": policy.mode.value,
                            "estimated_cost": cost,
                            "session_expires_at": session_expires_at,
                        }
                    )

                    mapping_result = await self.vault.get(user_id, session_id)
                    if not mapping_result.ok:
                        yield _sse(
                            {
                                "type": "error",
                                "content": mapping_result.message,
                                "code": mapping_result.code,
                            }
                        )
                        await _log_stream_event(False, mapping_result.code)
                        return
                    mapping: Dict[str, str] = mapping_result.value or {}

                    raw_llm_parts: List[str] = []
                    restored_parts: List[str] = []
                    buffer = ""
                    last_chunk_at = time.monotonic()

                    async for chunk in self.router.stream(
                        sanitised_messages,
                        model_used,
                        max_tokens=max_tokens,
                        system=system,
                        provider=None if selection else provider_name,
                    ):
                        raw_llm_parts.append(chunk)
                        now = time.monotonic()

                        if buffer and (now - last_chunk_at) > _BUFFER_TIMEOUT:
                            restored = _apply_mapping(buffer, mapping)
                            if restored:
                                restored_parts.append(restored)
                                yield _sse({"type": "delta", "content": restored})
                            buffer = ""

                        last_chunk_at = now
                        buffer += chunk

                        safe_part, partial_suffix = _split_at_partial(buffer)
                        if safe_part:
                            restored = _apply_mapping(safe_part, mapping)
                            if restored:
                                restored_parts.append(restored)
                                yield _sse({"type": "delta", "content": restored})
                        buffer = partial_suffix

                    yield _sse({"type": "status", "step": "restoring"})
                    if buffer:
                        restored = _apply_mapping(buffer, mapping)
                        if restored:
                            restored_parts.append(restored)
                            yield _sse({"type": "delta", "content": restored})

                    raw_response = "".join(raw_llm_parts)
                    full_restored = "".join(restored_parts)
                    prompt_text = "".join(m.get("content", "") for m in sanitised_messages)
                    input_tokens = estimate_tokens(prompt_text, model_used or model_display)
                    output_tokens = estimate_tokens(raw_response, model_used or model_display)
                    model_meta = MODEL_REGISTRY.get(model_display) or MODEL_REGISTRY.get(model_used)
                    tracked_cost = round(
                        (input_tokens / 1000) * model_meta["price_per_1k_input"]
                        + (output_tokens / 1000) * model_meta["price_per_1k_output"],
                        6,
                    ) if model_meta else 0.0

                    asyncio.ensure_future(
                        track_usage(
                            user_id=user_id,
                            org_id=org_id,
                            model=model_used,
                            provider=provider_name,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cost_usd=tracked_cost,
                            request_type="stream",
                            session_id=session_id,
                        )
                    )

                    if project:
                        yield _sse({"type": "status", "step": "saving"})
                        user_original = messages_to_pseudonymize[0]["content"] if messages_to_pseudonymize else ""
                        await project_manager.update_activity(
                            project=project,
                            user_message_redacted=new_pseudonymised[-1]["content"] if new_pseudonymised else "",
                            user_message_restored=_build_restored_user_message(user_original, attachment_name),
                            assistant_redacted=raw_response,
                            assistant_restored=full_restored,
                        )

                    logger.debug(
                        "chat_service.stream.done | session=%s | model=%s",
                        session_id,
                        model_used,
                    )
                    yield _sse({"type": "status", "step": "done"})
                    await _log_stream_event(True)
                    yield _sse({"type": "done"})
            except asyncio.TimeoutError:
                logger.error(
                    "chat_service.stream_timeout | project=%s | user=%s",
                    project_id or "",
                    user_id,
                )
                yield _sse({"type": "error", "content": "Timeout - reessayez", "code": "STREAM_TIMEOUT"})
                await _log_stream_event(False, "STREAM_TIMEOUT")
                return
            except Exception as exc:
                from app.services.router_adapter import normalize_llm_exception  # noqa: PLC0415

                message, code = normalize_llm_exception(
                    exc,
                    provider_name if provider_name in _VALID_PROVIDERS else provider,
                    model_used or model or "",
                )
                logger.error(
                    "chat_service.stream ERREUR | project=%s | user=%s | %s | trace=%s",
                    project_id or "",
                    user_id,
                    type(exc).__name__,
                    traceback.format_exc(),
                )
                yield _sse({"type": "error", "content": message, "code": code})
                await _log_stream_event(False, code)
                return

        return _event_generator()

    async def _resolve_model(
        self,
        *,
        messages: List[dict],
        provider: Optional[str],
        model: Optional[str],
        preferred_model: Optional[str],
        attachment_types: Optional[List[str]] = None,
    ) -> tuple[str, str, str, Optional[ModelSelection]]:
        if not settings.ROUTER_ENABLED:
            provider_name = (provider or settings.LLM_PROVIDER).lower()
            model_name = model or settings.DEFAULT_MODEL
            if provider_name not in _VALID_PROVIDERS:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Provider inconnu: {provider_name!r}",
                )
            return provider_name, model_name, model_name, None

        try:
            if preferred_model:
                selection = await select_model(None, user_preference=preferred_model)
            else:
                last_user_text = next(
                    (message.get("content", "") for message in reversed(messages) if message.get("role") == "user"),
                    "",
                )
                profile = await analyze_request(
                    last_user_text,
                    attachment_types=attachment_types,
                )
                selection = await select_model(profile)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            logger.warning("chat_service.resolve_model WARN | %s", type(exc).__name__)
            provider_name = (provider or settings.LLM_PROVIDER).lower()
            model_name = model or settings.DEFAULT_MODEL
            return provider_name, model_name, model_name, None

        return selection.provider, selection.model_id, selection.litellm_model_id, selection

    async def _check_org_policies(self, *, user_id: str, org_id: str, provider: str):
        from app.core.org_manager import OrgNotFoundError, get_org_manager  # noqa: PLC0415

        if not org_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Acces refuse: votre compte n'est rattache a aucune organisation. "
                    "Contactez votre administrateur."
                ),
            )

        mgr = get_org_manager()
        try:
            org_settings = await mgr.get_settings(org_id)
        except OrgNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Organisation introuvable ou desactivee. Contactez votre administrateur.",
            ) from exc

        if org_settings.allowed_providers and provider not in org_settings.allowed_providers:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Fournisseur {provider!r} non autorise par la politique de l'organisation.",
            )

        if org_settings.max_tokens_per_user > 0:
            usage = await get_monthly_usage(user_id)
            if usage.total_tokens >= org_settings.max_tokens_per_user:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Quota mensuel atteint ({org_settings.max_tokens_per_user} tokens).",
                )

        return org_settings

    @staticmethod
    def _get_project_manager():
        from app.core.project_manager import ProjectManager  # noqa: PLC0415

        return ProjectManager()
