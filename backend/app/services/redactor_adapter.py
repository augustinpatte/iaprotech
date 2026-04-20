"""
PresidioRedactorAdapter — adapte Redactor (core) vers RedactorInterface.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from app.core.interfaces import RedactorInterface
from app.core.result import Result, ok, err
from app.utils.logger import get_logger

logger = get_logger(__name__)


class PresidioRedactorAdapter(RedactorInterface):
    """Adapte get_shared_redactor() vers RedactorInterface."""

    async def pseudonymize(
        self,
        text: str,
        language: str = "auto",
        entities: Optional[List[str]] = None,
        score_threshold: float = 0.6,
        excluded_entity_types: Optional[List[str]] = None,
    ) -> Result:
        try:
            from app.core.redactor import get_shared_redactor_async  # noqa: PLC0415

            redactor = await get_shared_redactor_async()
            pseudonymized, mapping = await redactor.smart_pseudonymize(
                text,
                entities=entities,
                score_threshold=score_threshold,
                excluded_entity_types=excluded_entity_types,
            )
            return ok((pseudonymized, mapping))
        except Exception as exc:
            logger.error("redactor.pseudonymize ERREUR | %s", type(exc).__name__)
            return err(f"Erreur pseudonymisation: {type(exc).__name__}", "REDACTOR_ERROR")

    async def reidentify(self, text: str, mapping: Dict[str, str]) -> Result:
        try:
            import re  # noqa: PLC0415
            import re as _re
            _PH_RE = _re.compile(r"\[[A-Z_]+_\d+\]")
            restored = _PH_RE.sub(lambda m: mapping.get(m.group(0), m.group(0)), text)
            return ok(restored)
        except Exception as exc:
            logger.error("redactor.reidentify ERREUR | %s", type(exc).__name__)
            return err(f"Erreur re-identification: {type(exc).__name__}", "REDACTOR_ERROR")
