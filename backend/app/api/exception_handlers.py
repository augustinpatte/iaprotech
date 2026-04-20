"""
Gestion d'erreurs unifiee — un handler par type d'erreur, jamais de details
techniques exposes au client. Chaque handler logue l'erreur avec IDs haches.

Enregistrement dans main.py :
    from app.api.exception_handlers import register_exception_handlers
    register_exception_handlers(app)
"""
from __future__ import annotations

import hashlib
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.utils.logger import get_logger

logger = get_logger(__name__)


def _h(v: Any) -> str:
    """Hash un identifiant pour les logs (RGPD)."""
    return hashlib.sha256(str(v).encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Exceptions metier
# ---------------------------------------------------------------------------

class AuthenticationError(Exception):
    """Token manquant, invalide ou expire."""


class PermissionError(Exception):
    """Acces refuse (role insuffisant, org policy, etc.)."""


class RateLimitError(Exception):
    """Quota depasse."""


class VaultError(Exception):
    """Vault indisponible (circuit ouvert, Redis down)."""


class LLMError(Exception):
    """Erreur du fournisseur LLM."""


class ProjectNotFoundError(Exception):
    """Projet introuvable pour cet utilisateur."""


class ServiceUnavailableError(Exception):
    """Service interne indisponible (Redis, tracker, org manager, etc.)."""


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    logger.info(
        "validation_error | path=%s | errors=%d",
        request.url.path, len(exc.errors()),
    )
    # Retourner les erreurs Pydantic (pas de donnees sensibles dedans)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"error": {"code": "VALIDATION_ERROR", "details": exc.errors()}},
    )


async def handle_authentication_error(request: Request, exc: AuthenticationError) -> JSONResponse:
    user_id = getattr(getattr(request, "state", None), "user_id", "?")
    logger.warning(
        "auth_error | path=%s | user=%s | msg=%s",
        request.url.path, _h(user_id), str(exc),
    )
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content=_error_body("AUTHENTICATION_ERROR", "Authentification requise."),
        headers={"WWW-Authenticate": "Bearer"},
    )


async def handle_permission_error(request: Request, exc: PermissionError) -> JSONResponse:
    user_id = getattr(getattr(request, "state", None), "user_id", "?")
    logger.warning(
        "permission_error | path=%s | user=%s | msg=%s",
        request.url.path, _h(user_id), str(exc),
    )
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content=_error_body("PERMISSION_DENIED", "Acces refuse."),
    )


async def handle_rate_limit_error(request: Request, exc: RateLimitError) -> JSONResponse:
    logger.info("rate_limit | path=%s", request.url.path)
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content=_error_body("RATE_LIMIT_EXCEEDED", "Trop de requetes. Reessayez dans quelques secondes."),
        headers={"Retry-After": "60"},
    )


async def handle_vault_error(request: Request, exc: VaultError) -> JSONResponse:
    user_id = getattr(getattr(request, "state", None), "user_id", "?")
    logger.error(
        "vault_error | path=%s | user=%s | %s",
        request.url.path, _h(user_id), type(exc).__name__,
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=_error_body(
            "VAULT_UNAVAILABLE",
            "Service de chiffrement temporairement indisponible. Reessayez dans quelques instants.",
        ),
        headers={"Retry-After": "30"},
    )


async def handle_llm_error(request: Request, exc: LLMError) -> JSONResponse:
    user_id = getattr(getattr(request, "state", None), "user_id", "?")
    logger.error(
        "llm_error | path=%s | user=%s | %s",
        request.url.path, _h(user_id), type(exc).__name__,
    )
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content=_error_body(
            "LLM_ERROR",
            "Erreur du fournisseur LLM. Reessayez.",
        ),
    )


async def handle_project_not_found(request: Request, exc: ProjectNotFoundError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content=_error_body("PROJECT_NOT_FOUND", "Projet introuvable."),
    )


async def handle_service_unavailable(request: Request, exc: ServiceUnavailableError) -> JSONResponse:
    user_id = getattr(getattr(request, "state", None), "user_id", "?")
    logger.warning(
        "service_unavailable | path=%s | user=%s | msg=%s",
        request.url.path, _h(user_id), str(exc),
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=_error_body(
            "SERVICE_UNAVAILABLE",
            "Service temporairement indisponible. Reessayez dans quelques instants.",
        ),
        headers={"Retry-After": "30"},
    )


async def handle_generic_exception(request: Request, exc: Exception) -> JSONResponse:
    user_id = getattr(getattr(request, "state", None), "user_id", "?")
    logger.error(
        "unhandled_exception | path=%s | user=%s | exc=%s",
        request.url.path, _h(user_id), type(exc).__name__,
        exc_info=True,
    )
    # Jamais de details techniques au client
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_error_body(
            "INTERNAL_ERROR",
            "Une erreur interne est survenue. L'equipe technique a ete notifiee.",
        ),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Enregistre tous les handlers sur l'application FastAPI."""
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(ValidationError, handle_validation_error)
    app.add_exception_handler(AuthenticationError, handle_authentication_error)
    app.add_exception_handler(PermissionError, handle_permission_error)
    app.add_exception_handler(RateLimitError, handle_rate_limit_error)
    app.add_exception_handler(VaultError, handle_vault_error)
    app.add_exception_handler(LLMError, handle_llm_error)
    app.add_exception_handler(ProjectNotFoundError, handle_project_not_found)
    app.add_exception_handler(ServiceUnavailableError, handle_service_unavailable)
    app.add_exception_handler(Exception, handle_generic_exception)
