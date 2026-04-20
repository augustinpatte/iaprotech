"""
Validateurs reutilisables pour schemas Pydantic et routes FastAPI.

Usage dans un schema Pydantic :
    class MyRequest(BaseModel):
        session_id: Optional[str] = None
        model: Optional[str] = None
        message: str

        @validator("session_id", pre=True, always=True)
        def _check_session(cls, v):
            return validate_uuid4(v) if v else v

        @validator("model", pre=True, always=True)
        def _check_model(cls, v):
            return validate_model_name(v) if v else v

        @validator("message", pre=True)
        def _check_msg(cls, v):
            return validate_message_content(v)
"""
from __future__ import annotations
import re

# UUID v4 : 4 hex groupes, 3e groupe commence par 4, 4e groupe commence par [89ab]
_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Caracteres autorises dans un nom de modele
_MODEL_NAME_RE = re.compile(r"^[a-zA-Z0-9\-_./: ]+$")

_MAX_MESSAGE_LENGTH = 10_000
_MAX_MODEL_LENGTH = 128


def validate_uuid4(value: str) -> str:
    """Valide un UUID v4 strict. Leve ValueError si invalide."""
    if not value or not _UUID4_RE.match(value):
        raise ValueError(f"Format UUID v4 invalide : {value!r}")
    return value.lower()


def validate_model_name(value: str) -> str:
    """Valide que le nom de modele contient uniquement des caracteres autorises."""
    if not value:
        raise ValueError("Nom de modele vide.")
    if len(value) > _MAX_MODEL_LENGTH:
        raise ValueError(f"Nom de modele trop long : {len(value)} chars (max {_MAX_MODEL_LENGTH}).")
    if not _MODEL_NAME_RE.match(value):
        raise ValueError(f"Nom de modele contient des caracteres invalides : {value!r}")
    return value


def sanitize_text(value: str) -> str:
    """Strip et supprime les null bytes d'un texte. Ne leve jamais d'exception."""
    if not value:
        return ""
    # Supprimer null bytes (potentielle injection)
    value = value.replace("\x00", "")
    return value.strip()


def validate_message_content(value: str) -> str:
    """Valide et assainit le contenu d'un message utilisateur."""
    value = sanitize_text(value)
    if not value:
        raise ValueError("Le contenu du message ne peut pas etre vide.")
    if len(value) > _MAX_MESSAGE_LENGTH:
        raise ValueError(
            f"Message trop long : {len(value)} chars (max {_MAX_MESSAGE_LENGTH})."
        )
    return value


def validate_provider(value: str, allowed: frozenset) -> str:
    """Valide qu'un provider est dans la whitelist."""
    v = value.lower().strip()
    if v not in allowed:
        raise ValueError(f"Provider inconnu : {v!r}. Valeurs acceptees : {sorted(allowed)}.")
    return v
