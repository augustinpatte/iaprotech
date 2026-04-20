"""
Modeles Pydantic pour le systeme multi-utilisateurs par organisation.
"""

import uuid
from datetime import datetime, timezone
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from app.config import settings


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


class MemberWithUsage(BaseModel):
    user_id: str
    role: str
    joined_at: str
    is_active: bool
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    requests_count: int = 0
