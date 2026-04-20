"""
Modeles Pydantic pour les projets persistants.

Invariant de confidentialite (RGPD) :
  ProjectMessage.content_redacted  - UNIQUEMENT des placeholders pseudonymises
                                     ([PERSONNE_1], [EMAIL_2]...). Jamais de PII reelles.
                                     Inclus dans l'historique envoye au LLM.
  ProjectMessage.content_restored  - Donnees reelles re-identifiees.
                                     Affichage frontend UNIQUEMENT.
                                     Ne doit JAMAIS etre inclus dans l'historique LLM.
  Project.session_expires_at       - ISO 8601 UTC. Calcule a chaque update_activity
                                     comme now() + REDIS_TTL_SECONDS. Apres cette date
                                     le vault a expire ; la session ne peut pas etre
                                     continuee (HTTP 410). Aucune restauration possible.
  GARANTIE RGPD : les PII ne sont JAMAIS persistees dans le projet (ni en clair
                  ni chiffrees). Seuls les placeholders pseudonymises et les
                  donnees re-identifiees pour affichage sont stockes.

Statuts :
  active   -- conversation en cours
  archived -- inactif depuis 30 jours
  deleted  -- supprime RGPD (irreversible)
"""

import uuid
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class ProjectMessage(BaseModel):
    role: Literal["user", "assistant"]
    # Placeholders pseudonymises UNIQUEMENT ([PERSONNE_1]...)
    # -> inclus dans l'historique envoye au LLM. Ne contient jamais de PII reelles.
    content_redacted: str
    # Donnees reelles apres re-identification.
    # -> affichage frontend uniquement. Ne doit JAMAIS partir au LLM.
    content_restored: str
    timestamp: str  # ISO 8601 UTC


class Project(BaseModel):
    project_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    name: str
    created_at: str = Field(default="")
    last_activity: str = Field(default="")
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    # Expiration du vault Redis (now + REDIS_TTL_SECONDS au dernier update_activity).
    # None = nouveau projet sans messages. Apres cette date : HTTP 410, pas de restauration.
    session_expires_at: Optional[str] = None
    messages: List[ProjectMessage] = Field(default_factory=list)
    messages_count: int = 0
    status: Literal["active", "archived", "deleted"] = "active"


class ProjectMessagePublic(BaseModel):
    """Message tel que retourne par l'API publique.
    content_restored est EXCLU : les donnees re-identifiees ne quittent jamais le backend.
    """
    role: Literal["user", "assistant"]
    content_redacted: str
    timestamp: str


class ProjectSummary(BaseModel):
    project_id: str
    name: str
    created_at: str
    last_activity: str
    messages_count: int
    status: str


class ProjectListResponse(BaseModel):
    projects: List[ProjectSummary]
    total: int


class ProjectDetailResponse(BaseModel):
    project_id: str
    name: str
    created_at: str
    last_activity: str
    messages_count: int
    status: str
    session_id: str
    messages: List[ProjectMessagePublic]


class ProjectDeletedResponse(BaseModel):
    project_id: str
    deleted: bool
    message: str


class ProjectArchiveResponse(BaseModel):
    project_id: str
    status: str
    message: str
