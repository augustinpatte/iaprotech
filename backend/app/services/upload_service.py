"""
UploadService — orchestre file_processor + vault pour le traitement de fichiers.
"""
from __future__ import annotations

from typing import Optional

from app.core.interfaces import VaultInterface, RedactorInterface
from app.core.result import Result, ok, err
from app.utils.logger import get_logger

logger = get_logger(__name__)


class UploadService:
    """Orchestre l'extraction de fichier + pseudonymisation + vault."""

    def __init__(self, vault: VaultInterface, redactor: RedactorInterface) -> None:
        self.vault = vault
        self.redactor = redactor

    async def process_file(
        self,
        file_bytes: bytes,
        filename: str,
        user_id: str,
        session_id: str,
        entities: Optional[list] = None,
    ) -> Result:
        """
        Extrait le texte du fichier, pseudonymise, stocke le mapping.
        Retourne Ok({"text": pseudonymized_text, "entities_redacted": int}) | Err(...).
        """
        try:
            from app.core.file_processor import extract_text  # noqa: PLC0415
        except ImportError as exc:
            return err(f"file_processor non disponible: {exc}", "UPLOAD_DEPENDENCY_ERROR")

        try:
            text = await extract_text(file_bytes, filename)
        except Exception as exc:
            logger.error("upload_service.extract ERREUR | file=%s | %s", filename, type(exc).__name__)
            return err(f"Extraction fichier echouee: {type(exc).__name__}", "EXTRACT_ERROR")

        result = await self.redactor.pseudonymize(text, entities=entities)
        if not result.ok:
            return result

        pseudonymized, mapping = result.value
        if mapping:
            store_result = await self.vault.store(user_id, session_id, mapping)
            if not store_result.ok:
                return store_result

        return ok({"text": pseudonymized, "entities_redacted": len(mapping)})
