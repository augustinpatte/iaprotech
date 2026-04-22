"""
Modeles Pydantic pour le systeme multi-utilisateurs par organisation.
"""

import re
import uuid
from datetime import datetime, timezone
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.config import settings

_ALLOWED_PROVIDERS = frozenset({"anthropic", "openai", "google", "mistral"})
_FORBIDDEN_MODEL_RE = re.compile(r"^[a-zA-Z0-9_\-./:]{1,128}$")
_DEPARTMENT_RE = re.compile(r"^[a-zA-Z0-9_\- ]{1,64}$")
_REDACTION_ENTITIES = frozenset({
    "EMAIL",
    "PHONE_NUMBER",
    "IBAN",
    "CREDIT_CARD",
    "PERSON",
    "LOCATION",
    "NIR",
    "DATE_TIME",
    "URL",
    "IP_ADDRESS",
    "SALARY",
    "CONTRACT",
})


def _validate_allowed_providers_list(values: Optional[List[str]]) -> Optional[List[str]]:
    if values is None:
        return values
    for item in values:
        if item not in _ALLOWED_PROVIDERS:
            raise ValueError(
                f"allowed_providers contient une valeur invalide: {item!r}. "
                f"Valeurs autorisees: {sorted(_ALLOWED_PROVIDERS)}"
            )
    return values


def _validate_forbidden_models_list(values: List[str]) -> List[str]:
    for item in values:
        if not _FORBIDDEN_MODEL_RE.fullmatch(item):
            raise ValueError(
                f"forbidden_models contient une valeur invalide: {item!r}. "
                "Format autorise: ^[a-zA-Z0-9_\\-./:]{1,128}$"
            )
    return values


def _validate_departments_list(values: Optional[List[str]]) -> Optional[List[str]]:
    if values is None:
        return values
    for item in values:
        if not _DEPARTMENT_RE.fullmatch(item):
            raise ValueError(
                f"departments_strict_mode contient une valeur invalide: {item!r}. "
                "Format autorise: ^[a-zA-Z0-9_\\- ]{1,64}$"
            )
    return values


def _validate_redaction_entities_list(values: Optional[List[str]]) -> Optional[List[str]]:
    if values is None:
        return values
    for item in values:
        if item not in _REDACTION_ENTITIES:
            raise ValueError(
                f"redaction_entities contient une valeur invalide: {item!r}. "
                f"Valeurs autorisees: {sorted(_REDACTION_ENTITIES)}"
            )
    return values


class Organization(BaseModel):
    org_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    plan: Literal["starter", "business", "basic", "pro", "max"] = "basic"
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    owner_id: str


class OrgMember(BaseModel):
    org_id: str
    user_id: str
    role: Literal["admin", "member"] = "member"
    joined_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    is_active: bool = True


class OrgPolicy(BaseModel):
    min_protection_mode: str = "privacy"
    forbidden_models: List[str] = Field(default_factory=list)
    allowed_file_types: List[str] = Field(
        default_factory=lambda: list(settings.ALLOWED_FILE_TYPES)
    )
    max_file_size_mb: int = 10
    require_audit_for_export: bool = True
    departments_strict_mode: List[str] = Field(
        default_factory=lambda: ["finance", "rh", "legal"]
    )

    @field_validator("forbidden_models")
    @classmethod
    def validate_forbidden_models(cls, value: List[str]) -> List[str]:
        return _validate_forbidden_models_list(value)

    @field_validator("departments_strict_mode")
    @classmethod
    def validate_departments_strict_mode(cls, value: List[str]) -> List[str]:
        validated = _validate_departments_list(value)
        return [] if validated is None else validated


class OrgSettings(BaseModel):
    org_id: str
    allowed_providers: List[str] = Field(
        default_factory=lambda: ["anthropic", "openai", "google", "mistral"]
    )
    redaction_entities: List[str] = Field(default_factory=list)   # vide = utiliser defaults settings.py
    max_tokens_per_user: int = 0         # 0 = illimite (plafonner par le plan)
    policy: OrgPolicy = Field(default_factory=OrgPolicy)

    @property
    def min_mode(self) -> str:
        return self.policy.min_protection_mode

    @field_validator("allowed_providers")
    @classmethod
    def validate_allowed_providers(cls, value: List[str]) -> List[str]:
        validated = _validate_allowed_providers_list(value)
        return [] if validated is None else validated

    @field_validator("redaction_entities")
    @classmethod
    def validate_redaction_entities(cls, value: List[str]) -> List[str]:
        validated = _validate_redaction_entities_list(value)
        return [] if validated is None else validated


# ---------------------------------------------------------------------------
# Schemas de requete / reponse
# ---------------------------------------------------------------------------

class CreateOrgRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=80)
    plan: Literal["starter", "business", "basic", "pro", "max"] = "basic"


class InviteRequest(BaseModel):
    email: str = Field(..., max_length=256)
    role: Literal["admin", "member"] = "member"


class UpdateSettingsRequest(BaseModel):
    allowed_providers: Optional[List[str]] = None
    redaction_entities: Optional[List[str]] = None
    max_tokens_per_user: Optional[int] = None
    departments_strict_mode: Optional[List[str]] = None

    @field_validator("allowed_providers")
    @classmethod
    def validate_allowed_providers(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        return _validate_allowed_providers_list(value)

    @field_validator("redaction_entities")
    @classmethod
    def validate_redaction_entities(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        return _validate_redaction_entities_list(value)

    @field_validator("departments_strict_mode")
    @classmethod
    def validate_departments_strict_mode(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        return _validate_departments_list(value)


class MemberWithUsage(BaseModel):
    user_id: str
    role: str
    joined_at: str
    is_active: bool
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    requests_count: int = 0
