"""
ProjectManager -- gestion des sessions persistantes (projets).

Stockage Redis (fallback memoire) -- meme strategie que vault.py.
Cles : project:{id} -> JSON chiffre AES-256-GCM
       user_projects:{user} -> sorted set score=timestamp

Chiffrement AES-256-GCM avec VAULT_ENCRYPTION_KEY.
Logs : project_id + user_id uniquement -- zero contenu sensible.
"""

import asyncio
import base64
import json
import os
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import redis.asyncio as aioredis
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

from app.api.exception_handlers import ServiceUnavailableError
from app.config import settings
from app.models.project import Project, ProjectMessage, ProjectSummary
from app.utils.logger import get_logger, hash_id as _h

logger = get_logger(__name__)


class VaultDeletionError(Exception):
    """
    Leve quand la suppression vault echoue pendant delete_project.
    Garantit qu'un projet n'est jamais supprime si des PII subsistent.
    """

_SALT_SIZE = 16
_IV_SIZE = 12
_TAG_SIZE = 16
_PBKDF2_ITER = 100_000
_KEY_SIZE = 32
_BLOB_HEADER = _SALT_SIZE + _IV_SIZE + _TAG_SIZE


def _derive_key(master_key_bytes: bytes, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_SIZE,
        salt=salt,
        iterations=_PBKDF2_ITER,
    )
    return kdf.derive(master_key_bytes)


def encrypt_blob(plaintext: bytes, master_key_bytes: bytes) -> bytes:
    salt = os.urandom(_SALT_SIZE)
    iv = os.urandom(_IV_SIZE)
    key = _derive_key(master_key_bytes, salt)
    aesgcm = AESGCM(key)
    ct_tag = aesgcm.encrypt(iv, plaintext, None)
    ciphertext = ct_tag[:-_TAG_SIZE]
    tag = ct_tag[-_TAG_SIZE:]
    return salt + iv + tag + ciphertext


def decrypt_blob(blob: bytes, master_key_bytes: bytes) -> bytes:
    if len(blob) < _BLOB_HEADER:
        raise ValueError(f"Blob trop court ({len(blob)} B)")
    salt = blob[:_SALT_SIZE]
    iv = blob[_SALT_SIZE:_SALT_SIZE + _IV_SIZE]
    tag = blob[_SALT_SIZE + _IV_SIZE:_BLOB_HEADER]
    ciphertext = blob[_BLOB_HEADER:]
    key = _derive_key(master_key_bytes, salt)
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(iv, ciphertext + tag, None)


class _MemoryStore:
    def __init__(self) -> None:
        self._data: Dict[str, bytes] = {}
        self._sets: Dict[str, Dict[str, float]] = {}
        self._members: Dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def set(self, key: str, value: bytes) -> None:
        with self._lock:
            self._data[key] = value

    def get(self, key: str) -> Optional[bytes]:
        with self._lock:
            return self._data.get(key)

    def delete(self, key: str) -> bool:
        with self._lock:
            existed = key in self._data
            self._data.pop(key, None)
            return existed

    def zadd(self, key: str, score: float, member: str) -> None:
        with self._lock:
            if key not in self._sets:
                self._sets[key] = {}
            self._sets[key][member] = score

    def zrange_by_score_desc(self, key: str) -> List[str]:
        with self._lock:
            s = self._sets.get(key, {})
            return [m for m, _ in sorted(s.items(), key=lambda x: -x[1])]

    def zrem(self, key: str, member: str) -> None:
        with self._lock:
            if key in self._sets:
                self._sets[key].pop(member, None)

    def sadd(self, key: str, member: str) -> None:
        with self._lock:
            self._members.setdefault(key, set()).add(member)

    def smembers(self, key: str) -> List[str]:
        with self._lock:
            return sorted(self._members.get(key, set()))

    def delete_members(self, key: str) -> None:
        with self._lock:
            self._members.pop(key, None)

    def all_set_keys(self) -> List[str]:
        with self._lock:
            return list(self._sets.keys())


class ProjectManager:
    """Gestion CRUD des projets persistants. Fallback memoire si Redis indisponible."""

    def __init__(self) -> None:
        self._master_key: bytes = settings.VAULT_ENCRYPTION_KEY.encode("utf-8")
        self._redis: Optional[aioredis.Redis] = None
        self._redis_url: str = settings.REDIS_URL
        self._redis_available: bool = True
        self._mem = _MemoryStore() if settings.ENVIRONMENT == "development" else None
        logger.info("ProjectManager initialise")

    async def _get_redis(self) -> Optional[aioredis.Redis]:
        if self._redis is None:
            try:
                self._redis = aioredis.from_url(
                    self._redis_url,
                    encoding="utf-8",
                    decode_responses=False,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
                await self._redis.ping()
                self._redis_available = True
            except Exception as exc:
                logger.warning("ProjectManager: Redis inaccessible (%s)", exc)
                self._redis = None
                self._redis_available = False
                if self._mem is None:
                    raise ServiceUnavailableError("Service temporairement indisponible") from exc
        return self._redis if self._redis_available else None

    def _encode_project(self, project: Project) -> bytes:
        return encrypt_blob(project.model_dump_json().encode("utf-8"), self._master_key)

    async def _encode_project_async(self, project: Project) -> bytes:
        return await asyncio.get_running_loop().run_in_executor(
            None, self._encode_project, project
        )

    def _decode_project(self, blob: bytes) -> Project:
        data = json.loads(decrypt_blob(blob, self._master_key).decode("utf-8"))
        return Project(**data)

    async def _decode_project_async(self, blob: bytes) -> Project:
        return await asyncio.get_running_loop().run_in_executor(
            None, self._decode_project, blob
        )

    @staticmethod
    def _project_key(project_id: str) -> str:
        return f"project:{project_id}"

    @staticmethod
    def _project_sessions_key(project_id: str) -> str:
        return f"project:{project_id}:sessions"

    @staticmethod
    def _user_key(user_id: str) -> str:
        return f"user_projects:{user_id}"

    @staticmethod
    def _project_index_key(user_id: str) -> str:
        return f"index:projects:{user_id}"

    @staticmethod
    def _generate_name(first_message: str = "") -> str:  # noqa: ARG004
        # Le nom ne doit jamais deriver du contenu utilisateur (PII potentielle).
        # Un identifiant generique date + UUID court est utilise a la place.
        # L'utilisateur peut renommer le projet depuis l'UI.
        import uuid as _uuid  # noqa: PLC0415
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        uid = str(_uuid.uuid4())[:8]
        return f"Projet {date_str} \u2014 {uid}"

    async def create_project(
        self,
        user_id: str,
        first_message: str,
        session_id: Optional[str] = None,
    ) -> Project:
        now = datetime.now(timezone.utc).isoformat()
        project = Project(
            user_id=user_id,
            name=self._generate_name(first_message),
            created_at=now,
            last_activity=now,
            session_id=session_id or Project.model_fields["session_id"].default_factory(),
        )
        await self._save_project(project)
        await self.register_project_session(project.project_id, project.session_id)
        logger.info("project.create | project=%s | user=%s", _h(project.project_id), _h(user_id))
        return project

    async def get_project(
        self, project_id: str, user_id: str
    ) -> Optional[Project]:
        blob = await self._load_blob(project_id)
        if blob is None:
            return None
        try:
            project = await self._decode_project_async(blob)
        except Exception as exc:
            logger.error(
                "project.decode ERREUR | project=%s | %s", _h(project_id), type(exc).__name__
            )
            return None
        if project.user_id != user_id:
            logger.warning(
                "project.access_denied | project=%s | user=%s", _h(project_id), _h(user_id)
            )
            return None
        if project.status == "deleted":
            return None
        return project

    async def update_activity(
        self,
        project: Project,
        user_message_redacted: str,     # placeholders uniquement — jamais de PII
        user_message_restored: str,     # donnees reelles — affichage frontend uniquement
        assistant_redacted: str,        # reponse LLM brute pseudonymisee — historique LLM
        assistant_restored: str,        # reponse re-identifiee — affichage frontend uniquement
    ) -> None:
        # INVARIANT : assistant_redacted doit etre la sortie brute du LLM (pseudonymisee).
        # L'appelant ne doit JAMAIS passer la version re-identifiee comme assistant_redacted.
        # Violation = fuite PII au LLM au tour suivant via l'historique.
        now = datetime.now(timezone.utc)
        project.last_activity = now.isoformat()
        # session_expires_at = maintenant + TTL Redis : date a partir de laquelle
        # le vault aura expire et la session ne pourra plus etre continuee.
        project.session_expires_at = (
            now + timedelta(seconds=settings.REDIS_TTL_SECONDS)
        ).isoformat()
        project.messages.append(
            ProjectMessage(
                role="user",
                content_redacted=user_message_redacted,
                content_restored=user_message_restored,
                timestamp=project.last_activity,
            )
        )
        project.messages.append(
            ProjectMessage(
                role="assistant",
                content_redacted=assistant_redacted,
                content_restored=assistant_restored,
                timestamp=project.last_activity,
            )
        )
        project.messages_count = len(
            [m for m in project.messages if m.role == "user"]
        )
        await self._save_project(project)
        logger.debug(
            "project.update_activity | project=%s | messages=%d",
            _h(project.project_id), project.messages_count,
        )

    async def add_attachment_context(
        self,
        project: Project,
        attachment_name: str,
        redacted_text: str,
        excerpt_chars: int = 12000,
    ) -> None:
        safe_name = str(attachment_name or "").strip()
        if not safe_name:
            return

        excerpt = str(redacted_text or "").strip()
        if not excerpt:
            excerpt = "[Aucun texte extractible]"
        if len(excerpt) > excerpt_chars:
            excerpt = (
                f"{excerpt[:excerpt_chars]}\n\n"
                "[Document tronque pour limiter le contexte conversationnel]"
            )

        now = datetime.now(timezone.utc)
        project.last_activity = now.isoformat()
        project.session_expires_at = (
            now + timedelta(seconds=settings.REDIS_TTL_SECONDS)
        ).isoformat()
        project.messages.append(
            ProjectMessage(
                role="user",
                content_redacted=(
                    f"[Document joint: {safe_name}]\n\n"
                    f"Contenu du document joint pseudonymise :\n{excerpt}"
                ),
                content_restored=f"[Document joint: {safe_name}]",
                timestamp=project.last_activity,
            )
        )
        project.messages_count = len(
            [m for m in project.messages if m.role == "user"]
        )
        await self._save_project(project)
        logger.debug(
            "project.add_attachment_context | project=%s | attachment=%s",
            _h(project.project_id),
            safe_name,
        )

    async def list_projects(self, user_id: str) -> List[ProjectSummary]:
        r = await self._get_redis()
        user_key = self._user_key(user_id)
        if r:
            try:
                members = await r.zrevrange(user_key, 0, -1)
                project_ids = [
                    m.decode("utf-8") if isinstance(m, bytes) else m
                    for m in members
                ]
            except Exception as exc:
                logger.error(
                    "project.list ERREUR | user=%s | %s", _h(user_id), type(exc).__name__
                )
                return []
        elif self._mem is not None:
            project_ids = self._mem.zrange_by_score_desc(user_key)
        else:
            raise ServiceUnavailableError("Service temporairement indisponible")
        summaries = []
        for pid in project_ids:
            project = await self.get_project(pid, user_id)
            if project and project.status != "deleted":
                summaries.append(
                    ProjectSummary(
                        project_id=project.project_id,
                        name=project.name,
                        created_at=project.created_at,
                        last_activity=project.last_activity,
                        messages_count=project.messages_count,
                        status=project.status,
                    )
                )
        return summaries

    async def archive_project(self, project_id: str, user_id: str) -> bool:
        project = await self.get_project(project_id, user_id)
        if not project:
            return False
        # Purger le vault immediatement a l'archivage (RGPD : donnees non necessaires).
        # Best-effort : on continue meme si le vault est deja expire.
        if project.session_id:
            from app.core.vault import Vault  # noqa: PLC0415
            _v = Vault()
            try:
                await _v.delete(user_id=project.user_id, session_id=project.session_id)
                logger.info(
                    "project.archive.vault_purge | project=%s | rgpd=ok", _h(project_id)
                )
            except Exception as exc:
                logger.warning(
                    "project.archive.vault_purge WARN | project=%s | %s (vault peut deja etre expire)",
                    _h(project_id), type(exc).__name__,
                )
        project.status = "archived"
        project.session_expires_at = None  # vault purge
        # Purger content_restored de tous les messages (RGPD : donnees re-identifiees
        # ne doivent pas etre conservees apres archivage). content_redacted (placeholders)
        # est conserve pour l'historique pseudonymise.
        for msg in project.messages:
            msg.content_restored = ""
        await self._save_project(project)
        logger.info("project.archive | project=%s | user=%s", _h(project_id), _h(user_id))
        return True

    async def register_project_session(self, project_id: str, session_id: str) -> None:
        if not session_id:
            return
        sessions_key = self._project_sessions_key(project_id)
        r = await self._get_redis()
        if r:
            await r.sadd(sessions_key, session_id)
            return
        if self._mem is not None:
            self._mem.sadd(sessions_key, session_id)
            return
        raise ServiceUnavailableError("Service temporairement indisponible")

    async def get_project_session_ids(
        self,
        project_id: str,
        primary_session_id: str = "",
    ) -> List[str]:
        sessions: list[str] = []
        if primary_session_id:
            sessions.append(primary_session_id)

        sessions_key = self._project_sessions_key(project_id)
        r = await self._get_redis()
        if r:
            members = await r.smembers(sessions_key)
            sessions.extend(
                member.decode("utf-8") if isinstance(member, bytes) else member
                for member in members
            )
        elif self._mem is not None:
            sessions.extend(self._mem.smembers(sessions_key))
        else:
            raise ServiceUnavailableError("Service temporairement indisponible")

        deduped: list[str] = []
        seen: set[str] = set()
        for session_id in sessions:
            if session_id and session_id not in seen:
                seen.add(session_id)
                deduped.append(session_id)
        return deduped

    async def delete_project(self, project_id: str, user_id: str) -> bool:
        """
        Suppression RGPD Art. 17 — fail-closed.

        Ordre strict :
          1. Supprimer le vault (PII) pour chaque session du projet.
             Si la suppression vault echoue -> lever VaultDeletionError,
             NE PAS supprimer le projet (les PII subsistent, echec explicite).
          2. Seulement si vault OK -> supprimer le projet de Redis.

        Ne retourne jamais True si des donnees PII subsistent dans le vault.
        """
        project = await self.get_project(project_id, user_id)
        if not project:
            return False

        # --- etape 1 : suppression vault (fail-closed) ---
        from app.core.vault import Vault  # noqa: PLC0415
        _v = Vault()

        sessions_to_purge = await self.get_project_session_ids(
            project_id,
            primary_session_id=project.session_id,
        )

        for session_id in sessions_to_purge:
            try:
                result = await _v.delete(user_id=project.user_id, session_id=session_id)
                if not result.ok:
                    # Erreur explicite (circuit ouvert, Redis down...) — fail-closed
                    logger.error(
                        "project.delete vault ECHEC | project=%s | session=%s | err=%s | "
                        "SUPPRESSION PROJET ANNULEE — PII non purgees",
                        _h(project_id), _h(session_id), result.message,
                    )
                    raise VaultDeletionError(
                        f"Vault non supprime pour la session {_h(session_id)} : "
                        f"{result.message}"
                    )
                logger.info(
                    "project.delete vault | project=%s | session=%s | rgpd=ok",
                    _h(project_id), _h(session_id),
                )
            except VaultDeletionError:
                raise  # Propager immediatement
            except Exception as exc:
                logger.error(
                    "project.delete vault EXCEPTION | project=%s | session=%s | %s | "
                    "SUPPRESSION PROJET ANNULEE",
                    _h(project_id), _h(session_id), type(exc).__name__,
                )
                raise VaultDeletionError(
                    f"Exception inattendue lors de la purge vault : {type(exc).__name__}"
                ) from exc

        # --- etape 2 : supprimer le projet (vault confirme supprime) ---
        r = await self._get_redis()
        proj_key = self._project_key(project_id)
        user_key = self._user_key(project.user_id)
        try:
            if r:
                pipe = r.pipeline()
                pipe.delete(proj_key)
                pipe.delete(self._project_sessions_key(project_id))
                pipe.zrem(user_key, project_id)
                pipe.srem(self._project_index_key(project.user_id), project_id)
                pipe.srem("index:projects:all", project_id)
                await pipe.execute()
            elif self._mem is not None:
                self._mem.delete(proj_key)
                self._mem.delete_members(self._project_sessions_key(project_id))
                self._mem.zrem(user_key, project_id)
            else:
                raise ServiceUnavailableError("Service temporairement indisponible")
            logger.info(
                "project.delete | project=%s | user=%s | rgpd=ok",
                _h(project_id), _h(user_id),
            )
            return True
        except Exception as exc:
            logger.error(
                "project.delete projet ERREUR | project=%s | %s",
                _h(project_id), type(exc).__name__,
            )
            return False

    async def archive_inactive_projects(self) -> int:
        threshold = datetime.now(timezone.utc) - timedelta(days=settings.PROJECT_TTL_DAYS)
        archived_count = 0
        r = await self._get_redis()
        if r:
            try:
                keys = []
                async for key in r.scan_iter("user_projects:*"):
                    keys.append(key.decode("utf-8") if isinstance(key, bytes) else key)
            except Exception as exc:
                logger.error("project.archive_inactive SCAN ERREUR | %s", exc)
                return 0
        elif self._mem is not None:
            keys = [
                k for k in self._mem.all_set_keys()
                if k.startswith("user_projects:")
            ]
        else:
            raise ServiceUnavailableError("Service temporairement indisponible")
        for user_key in keys:
            user_id = user_key.removeprefix("user_projects:")
            if r:
                try:
                    members = await r.zrevrange(user_key, 0, -1)
                    project_ids = [
                        m.decode("utf-8") if isinstance(m, bytes) else m
                        for m in members
                    ]
                except Exception:
                    continue
            elif self._mem is not None:
                project_ids = self._mem.zrange_by_score_desc(user_key)
            else:
                raise ServiceUnavailableError("Service temporairement indisponible")
            for pid in project_ids:
                project = await self.get_project(pid, user_id)
                if not project or project.status != "active":
                    continue
                if datetime.fromisoformat(project.last_activity) < threshold:
                    # Appel a archive_project : purge le vault + archive le projet
                    await self.archive_project(pid, user_id)
                    archived_count += 1
                    logger.info(
                        "project.auto_archive | project=%s | user=%s | vault_purged=ok",
                        _h(pid), _h(user_id),
                    )
        if archived_count:
            logger.info("project.archive_inactive | archived=%d", archived_count)
        return archived_count


    async def rename_project(
        self, project_id: str, user_id: str, new_name: str
    ) -> Optional["Project"]:
        """Rename a project. Returns the updated Project, or None if not found."""
        project = await self.get_project(project_id, user_id)
        if not project:
            return None
        project.name = new_name
        await self._save_project(project)
        return project

    async def _save_project(self, project: Project) -> None:
        blob = await self._encode_project_async(project)
        proj_key = self._project_key(project.project_id)
        user_key = self._user_key(project.user_id)
        score = datetime.fromisoformat(project.last_activity).timestamp()
        r = await self._get_redis()
        try:
            if r:
                pipe = r.pipeline()
                pipe.set(proj_key, blob)
                pipe.zadd(user_key, {project.project_id: score})
                pipe.sadd(self._project_index_key(project.user_id), project.project_id)
                pipe.sadd("index:projects:all", project.project_id)
                await pipe.execute()
            elif self._mem is not None:
                self._mem.set(proj_key, blob)
                self._mem.zadd(user_key, score, project.project_id)
            else:
                raise ServiceUnavailableError("Service temporairement indisponible")
        except Exception as exc:
            logger.error(
                "project.save ERREUR | project=%s | %s",
                _h(project.project_id), type(exc).__name__,
            )
            raise

    async def _load_blob(self, project_id: str) -> Optional[bytes]:
        proj_key = self._project_key(project_id)
        r = await self._get_redis()
        try:
            if r:
                return await r.get(proj_key)
            if self._mem is not None:
                return self._mem.get(proj_key)
            raise ServiceUnavailableError("Service temporairement indisponible")
        except Exception as exc:
            logger.error(
                "project.load ERREUR | project=%s | %s",
                _h(project_id), type(exc).__name__,
            )
            return None
