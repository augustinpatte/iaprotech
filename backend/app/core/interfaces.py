"""
Interfaces abstraites (ABC) — decouple les implementations concretes des routes.

Les routes FastAPI et les services dependent uniquement de ces interfaces via Depends().
Les implementations concretes (Vault, Presidio Redactor, LiteLLM Router) les implementent.

Principe : les modules core ne connaissent pas les routes ni les services.
           Les services connaissent les interfaces, pas les implementations.
           Les routes connaissent les services, pas les modules core.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import AsyncGenerator, Dict, List, Optional

from app.core.result import Result


class VaultInterface(ABC):
    """Stockage chiffre des mappings token -> valeur originale."""

    @abstractmethod
    async def store(self, user_id: str, session_id: str, mapping: Dict[str, str]) -> Result:
        """Stocke ou fusionne un mapping chiffre. Ok(True) | Err(...)."""
        ...

    @abstractmethod
    async def get(self, user_id: str, session_id: str) -> Result:
        """Recupere un mapping. Ok(dict | None) | Err(...)."""
        ...

    @abstractmethod
    async def delete(self, user_id: str, session_id: str) -> Result:
        """Supprime une entree (RGPD Art. 17). Ok(bool) | Err(...)."""
        ...

    @abstractmethod
    async def restore(self, session_id: str, text: str, user_id: str = "") -> str:
        """Remplace les tokens dans text par leurs valeurs originales."""
        ...


class RedactorInterface(ABC):
    """Pseudonymisation et re-identification PII."""

    @abstractmethod
    async def pseudonymize(
        self,
        text: str,
        language: str = "auto",
        entities: Optional[List[str]] = None,
        score_threshold: float = 0.6,
        excluded_entity_types: Optional[List[str]] = None,
    ) -> Result:
        """Pseudonymise text. Ok((pseudonymized_text, mapping_dict)) | Err(...)."""
        ...

    @abstractmethod
    async def reidentify(self, text: str, mapping: Dict[str, str]) -> Result:
        """Remplace les tokens par leurs valeurs. Ok(restored_text) | Err(...)."""
        ...


class LLMRouterInterface(ABC):
    """Appels LLM multi-provider."""

    @abstractmethod
    async def call(
        self,
        messages: list,
        model: str,
        max_tokens: int = 2048,
        system: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> Result:
        """Appel LLM non-streaming. Ok(response_text) | Err(...)."""
        ...

    @abstractmethod
    async def stream(
        self,
        messages: list,
        model: str,
        max_tokens: int = 2048,
        system: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """Appel LLM streaming. Yields text chunks bruts."""
        ...
