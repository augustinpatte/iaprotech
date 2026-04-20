"""
ProjectService — orchestre project_manager + vault pour les projets persistants.
"""
from __future__ import annotations

from typing import Optional

from app.core.interfaces import VaultInterface
from app.core.result import Result, ok, err
from app.utils.logger import get_logger

logger = get_logger(__name__)


class ProjectService:
    """Orchestre project_manager + vault pour la persistance des projets."""

    def __init__(self, vault: VaultInterface) -> None:
        self.vault = vault

    async def get_or_create_project(
        self,
        project_id: Optional[str],
        user_id: str,
        first_message: str = "",
    ) -> Result:
        """
        Recupere un projet existant ou en cree un nouveau.
        Retourne Ok(project) | Err(...).
        """
        try:
            from app.core.project_manager import ProjectManager  # noqa: PLC0415
            pm = ProjectManager()
            if project_id:
                project = await pm.get_project(project_id, user_id)
                if project is None:
                    return err(f"Projet {project_id!r} introuvable", "PROJECT_NOT_FOUND")
                return ok(project)
            if first_message:
                project = await pm.create_project(user_id, first_message)
                return ok(project)
            return ok(None)
        except Exception as exc:
            logger.error("project_service.get_or_create ERREUR | %s", type(exc).__name__)
            return err(f"Erreur projet: {type(exc).__name__}", "PROJECT_ERROR")

