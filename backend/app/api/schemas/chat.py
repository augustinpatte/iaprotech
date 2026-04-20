"""
Schemas Pydantic stricts pour les routes /chat et /chat/stream.

Validation renforcee :
  - message : max 10 000 chars, pas de null bytes, strip
  - model   : caracteres autorises uniquement
  - session_id / project_id : UUID v4 strict
  - max_tokens : borne [1, 8192]
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, validator

from app.core.validators import (
    sanitize_text,
    validate_message_content,
    validate_model_name,
    validate_uuid4,
)


class Message(BaseModel):
    role: str = Field(..., pattern="^(user|assistant|system)$")
    content: str

    @validator("content", pre=True)
    def _validate_content(cls, v):  # noqa: N805
        return validate_message_content(v)


class ChatRequest(BaseModel):
    messages: List[Message] = Field(..., min_length=1, max_length=50)
    model: Optional[str] = None
    provider: Optional[str] = Field(default=None, max_length=32)
    protection_mode: str = Field(default="privacy", max_length=16)
    max_tokens: int = Field(default=2048, ge=1, le=8192)
    system: Optional[str] = Field(default=None, max_length=10_000)
    session_id: Optional[str] = None
    project_id: Optional[str] = None
    preferred_model: Optional[str] = None
    attachment_name: Optional[str] = Field(default=None, max_length=255)
    attachment_types: List[str] = Field(default_factory=list, max_items=8)

    @validator("model", pre=True, always=True)
    def _validate_model(cls, v):  # noqa: N805
        return validate_model_name(v) if v else v

    @validator("protection_mode", pre=True, always=True)
    def _validate_protection_mode(cls, v):  # noqa: N805
        value = (v or "privacy").strip().lower()
        if value not in {"fast", "privacy", "strict"}:
            raise ValueError("protection_mode doit etre fast, privacy ou strict")
        return value

    @validator("preferred_model", pre=True, always=True)
    def _validate_preferred_model(cls, v):  # noqa: N805
        return validate_model_name(v) if v else v

    @validator("session_id", pre=True, always=True)
    def _validate_session(cls, v):  # noqa: N805
        return validate_uuid4(v) if v else v

    @validator("project_id", pre=True, always=True)
    def _validate_project(cls, v):  # noqa: N805
        return validate_uuid4(v) if v else v

    @validator("system", pre=True, always=True)
    def _sanitize_system(cls, v):  # noqa: N805
        return sanitize_text(v) if v else v

    @validator("attachment_name", pre=True, always=True)
    def _sanitize_attachment_name(cls, v):  # noqa: N805
        return sanitize_text(v) if v else v

    @validator("attachment_types", pre=True, always=True)
    def _sanitize_attachment_types(cls, v):  # noqa: N805
        if not v:
            return []
        values = []
        for item in v:
            value = sanitize_text(item).lower().lstrip(".")
            if value:
                values.append(value)
        return values


class ChatStreamRequest(BaseModel):
    """Accepte soit 'message' (string) soit 'messages' (tableau)."""
    message: Optional[str] = None
    messages: Optional[List[Message]] = None
    model: Optional[str] = None
    provider: Optional[str] = Field(default=None, max_length=32)
    protection_mode: str = Field(default="privacy", max_length=16)
    max_tokens: int = Field(default=2048, ge=1, le=8192)
    system: Optional[str] = Field(default=None, max_length=10_000)
    session_id: Optional[str] = None
    project_id: Optional[str] = None
    preferred_model: Optional[str] = None
    attachment_name: Optional[str] = Field(default=None, max_length=255)
    attachment_types: List[str] = Field(default_factory=list, max_items=8)

    @validator("message", pre=True, always=True)
    def _validate_message(cls, v):  # noqa: N805
        if v is not None:
            return validate_message_content(v)
        return v

    @validator("messages", always=True)
    def resolve_messages(cls, v, values):  # noqa: N805
        if v is None and values.get("message"):
            return [Message(role="user", content=values["message"])]
        return v

    @validator("model", "preferred_model", pre=True, always=True)
    def _validate_models(cls, v):  # noqa: N805
        return validate_model_name(v) if v else v

    @validator("protection_mode", pre=True, always=True)
    def _validate_stream_protection_mode(cls, v):  # noqa: N805
        value = (v or "privacy").strip().lower()
        if value not in {"fast", "privacy", "strict"}:
            raise ValueError("protection_mode doit etre fast, privacy ou strict")
        return value

    @validator("session_id", "project_id", pre=True, always=True)
    def _validate_uuids(cls, v):  # noqa: N805
        return validate_uuid4(v) if v else v

    @validator("system", pre=True, always=True)
    def _sanitize_system(cls, v):  # noqa: N805
        return sanitize_text(v) if v else v

    @validator("attachment_name", pre=True, always=True)
    def _sanitize_attachment_name(cls, v):  # noqa: N805
        return sanitize_text(v) if v else v

    @validator("attachment_types", pre=True, always=True)
    def _sanitize_attachment_types(cls, v):  # noqa: N805
        if not v:
            return []
        values = []
        for item in v:
            value = sanitize_text(item).lower().lstrip(".")
            if value:
                values.append(value)
        return values


class ChatResponse(BaseModel):
    content: str
    session_id: str
    project_id: Optional[str] = None
    redacted_entities: int
    provider: str
    model: str
    model_used: str
    model_cost_estimate: Optional[float] = None


# Backward-compatible aliases for callers that still import the old names.
MessageSchema = Message
ChatRequestSchema = ChatRequest
ChatStreamRequestSchema = ChatStreamRequest
ChatResponseSchema = ChatResponse
