"""
Facade d'extraction de texte par type de fichier.

Le coeur des parseurs reste dans file_processor.py pour ne pas dupliquer la logique,
mais ce module fournit une interface stable et explicite pour l'upload.
"""
from __future__ import annotations

from typing import Optional

from app.core.file_processor import extract_text as _extract_text


async def extract_text(file_bytes: bytes, filename: str, mime_type: Optional[str] = None) -> str:
    # mime_type reserve pour des strategies futures; l'extension reste source de routage.
    return await _extract_text(file_bytes, filename)
