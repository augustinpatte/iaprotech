from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Set


class ProtectionMode(str, Enum):
    FAST = "fast"
    PRIVACY = "privacy"
    STRICT = "strict"


@dataclass
class PolicyConfig:
    mode: ProtectionMode
    active_entities: Set[str]
    detect_business_sensitive: bool
    min_confidence_score: float
    excluded_entity_types: Set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Politiques de base
# ---------------------------------------------------------------------------

FAST = PolicyConfig(
    mode=ProtectionMode.FAST,
    active_entities={"EMAIL", "PHONE_NUMBER", "IBAN", "CREDIT_CARD"},
    detect_business_sensitive=False,
    min_confidence_score=0.85,
)

PRIVACY = PolicyConfig(
    mode=ProtectionMode.PRIVACY,
    active_entities={"EMAIL", "PHONE_NUMBER", "IBAN", "PERSON", "LOCATION", "CREDIT_CARD", "NIR", "DATE_TIME"},
    detect_business_sensitive=False,
    min_confidence_score=0.75,
)

STRICT = PolicyConfig(
    mode=ProtectionMode.STRICT,
    active_entities={
        "EMAIL", "PHONE_NUMBER", "IBAN", "PERSON", "LOCATION",
        "CREDIT_CARD", "NIR", "DATE_TIME", "URL", "IP_ADDRESS",
    },
    detect_business_sensitive=True,
    min_confidence_score=0.65,
)

_ENTITY_ALIASES = {
    "EMAIL": "EMAIL_ADDRESS",
    "IBAN": "IBAN_CODE",
    "NIR": "FR_NIR",
}
_BUSINESS_SENSITIVE_ENTITIES = {"SALARY", "CONTRACT"}

_MODE_RANK = {"fast": 0, "privacy": 1, "strict": 2}
_POLICIES = {
    ProtectionMode.FAST.value: FAST,
    ProtectionMode.PRIVACY.value: PRIVACY,
    ProtectionMode.STRICT.value: STRICT,
}

PLAN_LIMITS = {
    "basic": {
        "max_invitations": 2,
        "max_tokens_month": 50000,
        "monthly_cost_budget_usd": 2.5,
        "max_users_in_org": 3,
        "models_allowed": ["claude-haiku-4-5", "gpt-4o-mini", "gemini-1.5-flash", "mistral-small-latest"],
        "protection_modes": ["fast", "privacy"],
        "max_file_size_mb": 5,
    },
    "pro": {
        "max_invitations": 10,
        "max_tokens_month": 300000,
        "monthly_cost_budget_usd": 15.0,
        "max_users_in_org": 20,
        "models_allowed": [
            "claude-haiku-4-5", "claude-sonnet-4-5",
            "gpt-4o", "gpt-4o-mini",
            "gemini-1.5-pro", "gemini-1.5-flash",
            "mistral-large-latest", "mistral-small-latest",
        ],
        "protection_modes": ["fast", "privacy", "strict"],
        "max_file_size_mb": 25,
    },
    "max": {
        "max_invitations": 50,
        "max_tokens_month": 1000000,
        "monthly_cost_budget_usd": 75.0,
        "max_users_in_org": 100,
        "models_allowed": "all",
        "protection_modes": ["fast", "privacy", "strict"],
        "max_file_size_mb": 100,
    },
}


def _normalize_entities(entities: Set[str], detect_business_sensitive: bool) -> Set[str]:
    normalized = {_ENTITY_ALIASES.get(entity, entity) for entity in entities}
    if detect_business_sensitive:
        normalized |= _BUSINESS_SENSITIVE_ENTITIES
    return normalized


def get_effective_policy(requested_mode: str, org_min_mode: str | None = None) -> PolicyConfig:
    requested = (requested_mode or ProtectionMode.PRIVACY.value).strip().lower()
    minimum = (org_min_mode or "").strip().lower() or None
    effective = requested
    if minimum and _MODE_RANK.get(minimum, 0) > _MODE_RANK.get(requested, 0):
        effective = minimum
    policy = deepcopy(_POLICIES.get(effective, PRIVACY))
    policy.active_entities = _normalize_entities(policy.active_entities, policy.detect_business_sensitive)
    return policy


def get_policy(mode: str, org_overrides: dict | None = None) -> PolicyConfig:
    """Alias de compatibilite pour les appelants existants."""
    org_min_mode = org_overrides.get("min_mode") if org_overrides else None
    return get_effective_policy(mode, org_min_mode)


def get_plan_limits(plan: str | None) -> dict:
    if not plan:
        return {}
    return deepcopy(PLAN_LIMITS.get(plan, {}))


FAST_POLICY = FAST
PRIVACY_POLICY = PRIVACY
STRICT_POLICY = STRICT
