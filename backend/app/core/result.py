"""
Result / Either pattern — gestion d'erreurs explicite sans exceptions silencieuses.

Usage:
    result = await vault.store(user_id, session_id, mapping)
    if not result.ok:
        logger.error("vault.store failed: %s [%s]", result.message, result.code)
        raise VaultError(result.message)
    # result.value disponible ici
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import TypeVar, Generic

T = TypeVar("T")


@dataclass
class Ok(Generic[T]):
    value: T
    ok: bool = field(default=True, init=False)


@dataclass
class Err:
    message: str
    code: str
    ok: bool = field(default=False, init=False)


Result = Ok | Err


def ok(value: T) -> "Ok[T]":
    """Wrap une valeur dans un Ok."""
    return Ok(value=value)


def err(message: str, code: str = "INTERNAL_ERROR") -> Err:
    """Cree un Err avec message et code d'erreur semantique."""
    return Err(message=message, code=code)
