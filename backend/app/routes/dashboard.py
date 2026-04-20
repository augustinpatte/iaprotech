"""
Dashboard routes -- consultation de l'usage LLM par utilisateur.

GET /dashboard/usage/current  -- agregat du mois en cours
GET /dashboard/usage/history  -- 30 derniers jours jour par jour (pour graphe)
GET /dashboard/usage/plan     -- plan + tokens alloues + % utilise
"""

import calendar
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request

from app.api.routes.auth import TokenData, _get_user, get_current_user
from app.config import settings
from app.core.router import MODEL_REGISTRY, calculate_cost
from app.core.policy_engine import get_plan_limits
from app.core.usage_tracker import get_daily_history, get_monthly_usage
from app.middleware.rate_limit import limiter
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = APIRouter()


def _build_model_breakdown(usage) -> dict:
    breakdown = {}
    for model_id, stats in usage.breakdown_by_model.items():
        meta = MODEL_REGISTRY.get(model_id, {})
        breakdown[model_id] = {
            "provider": meta.get("provider", ""),
            "tokens": int(stats.get("tokens", 0)),
            "cost_usd": round(float(stats.get("cost_usd", 0.0)), 6),
            "requests": int(stats.get("count", 0)),
            "count": int(stats.get("count", 0)),
            "price_input_per_1k": float(meta.get("price_per_1k_input", 0.0)),
            "price_output_per_1k": float(meta.get("price_per_1k_output", 0.0)),
        }
    return breakdown


def _build_provider_breakdown(usage) -> dict:
    breakdown = {}
    for provider, stats in usage.breakdown_by_provider.items():
        breakdown[provider] = {
            "tokens": int(stats.get("tokens", 0)),
            "cost_usd": round(float(stats.get("cost_usd", 0.0)), 6),
            "requests": int(stats.get("count", 0)),
            "count": int(stats.get("count", 0)),
        }
    return breakdown


def _build_pricing_catalog(usage) -> dict:
    catalog = {}
    for model_id, meta in MODEL_REGISTRY.items():
        stats = usage.breakdown_by_model.get(model_id, {})
        requests = int(stats.get("count", 0))
        observed_avg = round(float(stats.get("cost_usd", 0.0)) / requests, 6) if requests > 0 else None
        catalog[model_id] = {
            "provider": meta["provider"],
            "price_input_per_1k": float(meta["price_per_1k_input"]),
            "price_output_per_1k": float(meta["price_per_1k_output"]),
            "average_message_cost_usd": observed_avg if observed_avg is not None else calculate_cost(model_id, 500, 500),
            "observed_average_cost_usd": observed_avg,
            "requests": requests,
            "is_observed": observed_avg is not None,
        }
    return catalog


async def _resolve_user_plan(current_user: TokenData) -> tuple[str, dict]:
    user = await _get_user(current_user.username)
    plan_name = (user or {}).get("plan") or current_user.plan or settings.PLAN_NAME
    return plan_name, get_plan_limits(plan_name)


@router.get("/usage/current", summary="Usage du mois en cours")
@limiter.limit("60/minute")
async def usage_current(
    request: Request,
    current_user: TokenData = Depends(get_current_user),
):
    """Tokens, cout, nb requetes et breakdown detaille du mois courant."""
    usage = await get_monthly_usage(current_user.username)
    plan_name, plan_limits = await _resolve_user_plan(current_user)
    breakdown_by_model = _build_model_breakdown(usage)
    breakdown_by_provider = _build_provider_breakdown(usage)
    plan_limit_tokens = int(plan_limits.get("max_tokens_month") or settings.PLAN_MONTHLY_TOKENS)
    plan_limit_cost_usd = float(plan_limits.get("monthly_cost_budget_usd") or settings.PLAN_MONTHLY_COST_USD)

    percent_tokens_used = 0.0
    percent_cost_used = 0.0
    if plan_limit_tokens > 0:
        percent_tokens_used = round(usage.total_tokens / plan_limit_tokens * 100, 2)
    if plan_limit_cost_usd > 0:
        percent_cost_used = round(usage.total_cost_usd / plan_limit_cost_usd * 100, 2)

    cost_per_request_avg = round(
        usage.total_cost_usd / usage.requests_count,
        6,
    ) if usage.requests_count > 0 else 0.0

    now = datetime.now(timezone.utc)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    projected_monthly_cost = round(
        usage.total_cost_usd / now.day * days_in_month,
        6,
    ) if now.day > 0 else round(usage.total_cost_usd, 6)

    return {
        "total_tokens": usage.total_tokens,
        "total_cost_usd": usage.total_cost_usd,
        "requests_count": usage.requests_count,
        "breakdown_by_model": breakdown_by_model,
        "breakdown_by_provider": breakdown_by_provider,
        "by_model": breakdown_by_model,
        "by_provider": breakdown_by_provider,
        "plan_name": plan_name,
        "plan_limit_tokens": plan_limit_tokens,
        "plan_limit_cost_usd": plan_limit_cost_usd,
        "percent_tokens_used": percent_tokens_used,
        "percent_cost_used": percent_cost_used,
        "cost_per_request_avg": cost_per_request_avg,
        "projected_monthly_cost": projected_monthly_cost,
        "pricing_catalog": _build_pricing_catalog(usage),
        "estimation_note": "Provider usage when available, provider-specific estimate otherwise.",
    }


@router.get("/usage/history", summary="Historique jour par jour (30 derniers jours)")
@limiter.limit("60/minute")
async def usage_history(
    request: Request,
    current_user: TokenData = Depends(get_current_user),
):
    """Liste de 30 entrees (une par jour) avec date, tokens, cout et requetes."""
    daily = await get_daily_history(current_user.username, days=30)
    return {"days": daily}


@router.get("/usage/plan", summary="Plan actuel + quota utilise")
@limiter.limit("60/minute")
async def usage_plan(
    request: Request,
    current_user: TokenData = Depends(get_current_user),
):
    """Plan souscrit, quotas mensuels et pourcentages d'utilisation."""
    usage = await get_monthly_usage(current_user.username)
    plan_name, plan_limits = await _resolve_user_plan(current_user)
    monthly_tokens = int(plan_limits.get("max_tokens_month") or settings.PLAN_MONTHLY_TOKENS)
    monthly_requests = int(settings.PLAN_MONTHLY_REQUESTS)

    tokens_pct   = 0.0
    requests_pct = 0.0
    if monthly_tokens > 0:
        tokens_pct = round(usage.total_tokens / monthly_tokens * 100, 2)
    if monthly_requests > 0:
        requests_pct = round(usage.requests_count / monthly_requests * 100, 2)

    return {
        "plan_name":        plan_name,
        "monthly_tokens":   monthly_tokens,
        "monthly_requests": monthly_requests,
        "used_tokens":      usage.total_tokens,
        "used_requests":    usage.requests_count,
        "used_cost_usd":    usage.total_cost_usd,
        "tokens_pct":       tokens_pct,
        "requests_pct":     requests_pct,
    }
