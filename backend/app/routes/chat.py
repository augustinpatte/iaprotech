"""
Routes de chat -- coeur du proxy privacy.

POST /chat        : pseudonymisation -> LLM -> re-identification (reponse JSON)
POST /chat/stream : streaming SSE delegue a ChatService
GET  /models      : liste des modeles disponibles
"""

from __future__ import annotations

import asyncio
import traceback
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.api.routes.auth import TokenData, get_current_user, get_user_provider_api_keys
from app.api.schemas.chat import ChatRequest, ChatResponse, ChatStreamRequest
from app.config import settings
from app.core.audit_trail import AuditEvent, get_audit_trail
from app.core.interfaces import LLMRouterInterface, RedactorInterface, VaultInterface
from app.core.policy_engine import get_effective_policy
from app.middleware.rate_limit import limiter
from app.services.chat_service import ChatService
from app.utils.logger import get_logger, hash_id as _h

logger = get_logger(__name__)
router = APIRouter()

_vault_instance: Optional[VaultInterface] = None
_redactor_instance: Optional[RedactorInterface] = None
_router_instance: Optional[LLMRouterInterface] = None
_chat_service_instance: Optional[ChatService] = None

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


def get_vault() -> VaultInterface:
    global _vault_instance
    if _vault_instance is None:
        from app.core.vault import Vault  # noqa: PLC0415

        _vault_instance = Vault()
    return _vault_instance


def get_redactor() -> RedactorInterface:
    global _redactor_instance
    if _redactor_instance is None:
        from app.services.redactor_adapter import PresidioRedactorAdapter  # noqa: PLC0415

        _redactor_instance = PresidioRedactorAdapter()
    return _redactor_instance


def get_llm_router() -> LLMRouterInterface:
    global _router_instance
    if _router_instance is None:
        from app.services.router_adapter import LiteLLMRouterAdapter  # noqa: PLC0415

        _router_instance = LiteLLMRouterAdapter()
    return _router_instance


def get_chat_service() -> ChatService:
    global _chat_service_instance
    if _chat_service_instance is None:
        _chat_service_instance = ChatService(
            vault=get_vault(),
            redactor=get_redactor(),
            router=get_llm_router(),
        )
    return _chat_service_instance


def get_project_manager():
    from app.core.project_manager import ProjectManager  # noqa: PLC0415

    return ProjectManager()


def _ensure_model_allowed(forbidden_models: list[str], *candidates: Optional[str]) -> None:
    blocked = {model for model in forbidden_models if model}
    for candidate in candidates:
        if candidate and candidate in blocked:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Modele {candidate!r} interdit par la politique de l'organisation.",
            )


def _require_org_membership(current_user: TokenData) -> str:
    org_id = current_user.org_id or ""
    if not org_id:
        if current_user.role == "admin":
            return ""
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Compte non rattache a une organisation. Contactez votre administrateur.",
        )
    return org_id


async def _safe_audit_log(event: AuditEvent) -> None:
    try:
        await get_audit_trail().log(event)
    except Exception as exc:
        logger.warning("chat.audit WARN | %s", type(exc).__name__)


async def _resolve_model(
    payload: ChatRequest,
    provider_api_keys: Optional[Dict[str, str]] = None,
) -> tuple[str, str, Optional[object]]:
    from app.core.router import analyze_request, select_model  # noqa: PLC0415

    valid_providers = frozenset({"anthropic", "openai", "google", "mistral"})
    if not settings.ROUTER_ENABLED:
        provider = (payload.provider or settings.LLM_PROVIDER).lower()
        model = payload.model or settings.DEFAULT_MODEL
        if provider not in valid_providers:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Provider inconnu : {provider!r}. Valeurs acceptees : {sorted(valid_providers)}.",
            )
        return provider, model, None

    try:
        if payload.preferred_model:
            selection = await select_model(
                None,
                user_preference=payload.preferred_model,
                provider_api_keys=provider_api_keys,
            )
        else:
            last_user_text = next(
                (message.content for message in reversed(payload.messages) if message.role == "user"),
                "",
            )
            profile = await analyze_request(
                last_user_text,
                attachment_types=payload.attachment_types,
            )
            selection = await select_model(profile, provider_api_keys=provider_api_keys)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("router.select_model ERREUR %s -- fallback settings", type(exc).__name__)
        provider = (payload.provider or settings.LLM_PROVIDER).lower()
        model = payload.model or settings.DEFAULT_MODEL
        return provider, model, None

    return selection.provider, selection.model_id, selection


async def _check_org_policies(user_id: str, org_id: str, provider: str):
    from app.core.org_manager import OrgNotFoundError, get_org_manager  # noqa: PLC0415
    from app.core.usage_tracker import get_monthly_usage  # noqa: PLC0415

    try:
        org_settings = await get_org_manager().get_settings(org_id)
    except OrgNotFoundError as exc:
        logger.warning(
            "chat.org_policy_denied | user=%s | org=%s | reason=org_not_found",
            _h(user_id),
            _h(org_id),
        )
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


def _build_default_org_settings():
    from app.models.organization import OrgSettings  # noqa: PLC0415

    return OrgSettings(org_id="")


def _require_admin(current_user: TokenData) -> None:
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acces reserve aux administrateurs.",
        )


async def get_effective_mode(
    user_id: str,
    org_id: str,
    requested_mode: str,
    project_name: Optional[str] = None,
    org_settings: Optional[object] = None,
) -> str:
    if not org_id:
        return get_effective_policy(requested_mode, None).mode.value

    current_settings = org_settings
    if current_settings is None:
        from app.core.org_manager import OrgNotFoundError, get_org_manager  # noqa: PLC0415

        try:
            current_settings = await get_org_manager().get_settings(org_id)
        except OrgNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Organisation introuvable ou desactivee. Contactez votre administrateur.",
            ) from exc

    strict_departments = getattr(
        getattr(current_settings, "policy", None),
        "departments_strict_mode",
        [],
    ) or []
    if project_name:
        project_name_lower = project_name.lower()
        for department in strict_departments:
            if department and department.lower() in project_name_lower:
                if requested_mode != "strict":
                    logger.info(
                        "chat.mode_forced_strict | user=%s | org=%s | department=%s",
                        _h(user_id),
                        _h(org_id),
                        department.lower(),
                    )
                return "strict"

    minimum_mode = getattr(current_settings, "min_mode", None)
    return get_effective_policy(requested_mode, minimum_mode).mode.value


@router.get(
    "/models",
    summary="Liste tous les modeles disponibles avec prix et capacites",
)
@limiter.limit("120/minute")
async def list_models(
    request: Request,
    current_user: TokenData = Depends(get_current_user),
) -> Dict[str, Any]:
    from app.core.router import MODEL_REGISTRY, _provider_has_key  # noqa: PLC0415

    provider_api_keys = await get_user_provider_api_keys(current_user.username)
    models_out = {}
    for model_id, meta in MODEL_REGISTRY.items():
        provider = meta["provider"]
        models_out[model_id] = {
            **{key: value for key, value in meta.items() if key != "litellm_model_id"},
            "available": _provider_has_key(provider, provider_api_keys),
        }
    return {
        "router_enabled": settings.ROUTER_ENABLED,
        "default_analyzer": settings.DEFAULT_COMPLEXITY_ANALYZER,
        "models": models_out,
    }


@router.get("/debug/test-anthropic", summary="Test minimal Anthropic (debug admin)")
async def debug_test_anthropic(
    current_user: TokenData = Depends(get_current_user),
) -> Dict[str, Any]:
    _require_admin(current_user)

    from app.core.router import call_llm_via_litellm, mask_secret, resolve_provider_model  # noqa: PLC0415

    configured_model = settings.DEFAULT_MODEL if settings.DEFAULT_MODEL.startswith("claude-") else "claude-sonnet-4-5"
    resolved_model = resolve_provider_model("anthropic", configured_model)

    try:
        result = await call_llm_via_litellm(
            litellm_model_id=resolved_model,
            messages=[{"role": "user", "content": "Say hello"}],
            max_tokens=32,
            system="Respond briefly.",
        )
        return {
            "ok": True,
            "provider": "anthropic",
            "configured_model": configured_model,
            "resolved_model": resolved_model,
            "api_key_masked": mask_secret(settings.ANTHROPIC_API_KEY),
            "result_preview": str(result.get("text", ""))[:200],
            "usage": result.get("usage"),
        }
    except Exception as exc:
        logger.error(
            "debug.test_anthropic_failed | user=%s | model=%s | resolved=%s | error=%s | trace=%s",
            _h(current_user.username),
            configured_model,
            resolved_model,
            str(exc),
            traceback.format_exc(),
        )
        return {
            "ok": False,
            "provider": "anthropic",
            "configured_model": configured_model,
            "resolved_model": resolved_model,
            "api_key_masked": mask_secret(settings.ANTHROPIC_API_KEY),
            "error_type": type(exc).__name__,
            "error": "Erreur lors du traitement de la requête",
            "error_repr": type(exc).__name__,
        }


@router.post(
    "/chat",
    response_model=ChatResponse,
    status_code=status.HTTP_200_OK,
    summary="Chat LLM avec pseudonymisation PII",
)
@limiter.limit("60/minute")
async def chat(
    request: Request,
    payload: ChatRequest,
    current_user: TokenData = Depends(get_current_user),
    chat_service: ChatService = Depends(get_chat_service),
) -> ChatResponse:
    from app.core.router import MODEL_REGISTRY, calculate_cost, estimate_tokens  # noqa: PLC0415
    from app.core.usage_tracker import track_usage  # noqa: PLC0415

    org_id = _require_org_membership(current_user)
    project_id = payload.project_id
    model_used = ""
    effective_mode = payload.protection_mode
    total_entities = 0

    try:
        provider_api_keys = await get_user_provider_api_keys(current_user.username)
        provider, model_display, selection = await _resolve_model(payload, provider_api_keys)
        model_used = selection.litellm_model_id if selection else model_display

        project_manager = get_project_manager()
        project = await project_manager.get_project(payload.project_id, current_user.username) if payload.project_id else None

        org_settings = (
            await _check_org_policies(current_user.username, org_id, provider)
            if org_id else _build_default_org_settings()
        )
        org_policy = getattr(org_settings, "policy", None)
        effective_mode = await get_effective_mode(
            current_user.username,
            org_id,
            payload.protection_mode,
            project_name=project.name if project else None,
            org_settings=org_settings,
        )
        policy = get_effective_policy(effective_mode, getattr(org_policy, "min_protection_mode", "privacy"))
        _ensure_model_allowed(
            getattr(org_policy, "forbidden_models", []),
            payload.model,
            payload.preferred_model,
            model_display,
            model_used,
        )
        redaction_entities = org_settings.redaction_entities or list(policy.active_entities)

        if project:
            session_id = project.session_id
            if project.messages:
                mapping_result = await chat_service.vault.get(current_user.username, session_id)
                if not mapping_result.ok:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail=mapping_result.message,
                    )
                if mapping_result.value is None:
                    logger.info(
                        "chat.vault_expired | session=%s | project=%s | rgpd=ok",
                        _h(session_id),
                        _h(payload.project_id),
                    )
                    raise HTTPException(
                        status_code=status.HTTP_410_GONE,
                        detail=(
                            "Session expiree -- les donnees ont ete supprimees conformement "
                            "a la politique RGPD. Demarrez une nouvelle conversation."
                        ),
                    )
        else:
            session_id = str(uuid4())

        if project:
            last_user = next((message for message in reversed(payload.messages) if message.role == "user"), None)
            messages_to_pseudonymize = [last_user.model_dump()] if last_user else [message.model_dump() for message in payload.messages]
        else:
            messages_to_pseudonymize = [message.model_dump() for message in payload.messages]

        try:
            new_pseudonymised, total_entities = await chat_service.pseudonymize_messages(
                messages_to_pseudonymize,
                current_user.username,
                session_id,
                redaction_entities,
                score_threshold=policy.min_confidence_score,
                excluded_entity_types=list(policy.excluded_entity_types),
            )
        except RuntimeError as exc:
            raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=str(exc),
                ) from exc

        if project and project.messages:
            history_redacted = [{"role": message.role, "content": message.content_redacted} for message in project.messages]
            sanitised_messages = history_redacted + new_pseudonymised
        else:
            sanitised_messages = new_pseudonymised

        logger.info(
            "chat.start | session=%s | user=%s | entities=%d | model=%s | project=%s",
            _h(session_id),
            _h(current_user.username),
            total_entities,
            model_used,
            _h(payload.project_id) if payload.project_id else "none",
        )

        call_result = await chat_service.router.call(
            sanitised_messages,
            model_used,
            max_tokens=payload.max_tokens,
            system=payload.system,
            provider=None if selection else provider,
            provider_api_keys=provider_api_keys,
        )
        if not call_result.ok:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=call_result.message,
            )

        response_payload = call_result.value
        raw_response = response_payload.get("text", "") if isinstance(response_payload, dict) else str(response_payload or "")
        restored = await chat_service.vault.restore(session_id, raw_response, user_id=current_user.username)
        usage = response_payload.get("usage") if isinstance(response_payload, dict) else None
        prompt_text = "".join(message.get("content", "") for message in sanitised_messages)
        if usage:
            input_tokens = int(usage.get("prompt_tokens", 0) or 0)
            output_tokens = int(usage.get("completion_tokens", 0) or 0)
        else:
            input_tokens = estimate_tokens(prompt_text, model_used or model_display)
            output_tokens = estimate_tokens(raw_response, model_used or model_display)
        meta = MODEL_REGISTRY.get(model_display) or MODEL_REGISTRY.get(model_used)
        cost_usd = calculate_cost(model_display if meta and model_display in MODEL_REGISTRY else model_used, input_tokens, output_tokens) if meta else 0.0
        asyncio.ensure_future(
            track_usage(
                user_id=current_user.username,
                org_id=org_id,
                model=model_used,
                provider=provider,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                request_type="chat",
                session_id=session_id,
            )
        )

        if project:
            user_original = messages_to_pseudonymize[0]["content"] if messages_to_pseudonymize else ""
            await project_manager.update_activity(
                project=project,
                user_message_redacted=new_pseudonymised[-1]["content"] if new_pseudonymised else "",
                user_message_restored=_build_restored_user_message(user_original, payload.attachment_name),
                assistant_redacted=raw_response,
                assistant_restored=restored,
            )
        else:
            first_content = next((message.content for message in payload.messages if message.role == "user"), "")
            if first_content:
                project = await project_manager.create_project(
                    current_user.username,
                    first_content,
                    session_id=session_id,
                )
                await project_manager.update_activity(
                    project=project,
                    user_message_redacted=new_pseudonymised[-1]["content"] if new_pseudonymised else "",
                    user_message_restored=_build_restored_user_message(first_content, payload.attachment_name),
                    assistant_redacted=raw_response,
                    assistant_restored=restored,
                )

        cost = estimate_cost(model_display, sum(len(message.content) for message in payload.messages), payload.max_tokens) if selection else None
        response = ChatResponse(
            content=restored,
            session_id=session_id,
            project_id=project.project_id if project else None,
            redacted_entities=total_entities,
            provider=provider,
            model=model_display,
            model_used=model_used,
            model_cost_estimate=cost,
        )

        await _safe_audit_log(
            AuditEvent.build(
                user_id=current_user.username,
                org_id=org_id,
                project_id=response.project_id,
                action="chat",
                model_used=model_used,
                protection_mode=policy.mode.value,
                entities_masked=total_entities,
                file_type="chat",
                cost_usd=cost_usd,
                success=True,
            )
        )
        return response
    except HTTPException as exc:
        await _safe_audit_log(
            AuditEvent.build(
                user_id=current_user.username,
                org_id=org_id,
                project_id=project_id,
                action="chat",
                model_used=model_used,
                protection_mode=effective_mode,
                entities_masked=total_entities,
                file_type="chat",
                cost_usd=0.0,
                success=False,
                error_code=str(exc.status_code),
            )
        )
        raise
    except Exception as exc:
        await _safe_audit_log(
            AuditEvent.build(
                user_id=current_user.username,
                org_id=org_id,
                project_id=project_id,
                action="chat",
                model_used=model_used,
                protection_mode=effective_mode,
                entities_masked=total_entities,
                file_type="chat",
                cost_usd=0.0,
                success=False,
                error_code=type(exc).__name__,
            )
        )
        raise


@router.post(
    "/chat/stream",
    summary="Chat LLM en streaming SSE avec pseudonymisation PII",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/event-stream": {}}},
        400: {"description": "Parametres invalides"},
        401: {"description": "Token JWT manquant ou invalide"},
        429: {"description": "Trop de requetes"},
        502: {"description": "Erreur du fournisseur LLM"},
    },
)
@limiter.limit("60/minute")
async def chat_stream(
    request: Request,
    payload: ChatStreamRequest,
    current_user: TokenData = Depends(get_current_user),
    chat_service: ChatService = Depends(get_chat_service),
) -> StreamingResponse:
    org_id = _require_org_membership(current_user)
    effective_mode = payload.protection_mode
    project_manager = get_project_manager()
    project = await project_manager.get_project(payload.project_id, current_user.username) if payload.project_id else None
    org_settings = (
        await _check_org_policies(
            current_user.username,
            org_id,
            (payload.provider or settings.LLM_PROVIDER).lower(),
        )
        if org_id else _build_default_org_settings()
    )
    org_policy = getattr(org_settings, "policy", None)
    effective_mode = await get_effective_mode(
        current_user.username,
        org_id,
        payload.protection_mode,
        project_name=project.name if project else None,
        org_settings=org_settings,
    )
    stream_policy = get_effective_policy(effective_mode, getattr(org_policy, "min_protection_mode", "privacy"))
    effective_mode = stream_policy.mode.value
    _ensure_model_allowed(
        getattr(org_policy, "forbidden_models", []),
        payload.model,
        payload.preferred_model,
    )
    try:
        event_stream = await chat_service.stream_response(
            user_id=current_user.username,
            org_id=org_id,
            messages=[message.model_dump() for message in (payload.messages or [])],
            project_id=payload.project_id,
            model=payload.model,
            provider=payload.provider,
            max_tokens=payload.max_tokens,
            system=payload.system,
            preferred_model=payload.preferred_model,
            protection_mode=effective_mode,
            min_confidence_score=stream_policy.min_confidence_score,
            attachment_name=payload.attachment_name,
            attachment_types=payload.attachment_types,
        )
    except HTTPException as exc:
        await _safe_audit_log(
            AuditEvent.build(
                user_id=current_user.username,
                org_id=org_id,
                project_id=payload.project_id,
                action="chat",
                model_used=payload.preferred_model or payload.model or "",
                protection_mode=effective_mode,
                entities_masked=0,
                file_type="chat",
                cost_usd=0.0,
                success=False,
                error_code=str(exc.status_code),
            )
        )
        raise
    except Exception as exc:
        await _safe_audit_log(
            AuditEvent.build(
                user_id=current_user.username,
                org_id=org_id,
                project_id=payload.project_id,
                action="chat",
                model_used=payload.preferred_model or payload.model or "",
                protection_mode=effective_mode,
                entities_masked=0,
                file_type="chat",
                cost_usd=0.0,
                success=False,
                error_code=type(exc).__name__,
            )
        )
        raise
    return StreamingResponse(
        event_stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
