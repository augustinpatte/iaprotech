"""
Vault — stockage chiffre AES-256-GCM des mappings tokens <-> donnees reelles.

Architecture :
  - Chaque session recoit un UUID isole -> cle Redis : vault:{user_id}:{session_id}
  - Le mapping entier est serialise JSON, chiffre AES-256-GCM, stocke Redis + TTL
  - La cle AES est derivee par org via HKDF-SHA256, puis par blob via
    PBKDF2-HMAC-SHA256 avec sel aleatoire par entree (integre au blob chiffre)
  - Circuit breaker : 3 echecs Redis consecutifs -> OPEN (HTTP 503 explicite)
    Mode degrade jamais silencieux — toujours logue et signale.
  - Fallback memoire thread-safe UNIQUEMENT en CLOSED avec < 3 echecs (dev/test)
  - Implementer VaultInterface pour le decouplage via Depends()

Format blob chiffre (bytes concatenes) :
  v1 legacy: [ sel : 16 B ][ iv : 12 B ][ tag : 16 B ][ ciphertext : N B ]
  v2:        [ 0x02 : 1 B ][ key_version : 1 B ][ sel : 16 B ][ iv : 12 B ]
             [ tag : 16 B ][ ciphertext : N B ]

Logs : session_id uniquement — aucune donnee sensible exposee.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional

import redis.asyncio as aioredis
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from app.api.exception_handlers import VaultError
from app.config import settings
from app.core.result import Result, ok, err
from app.core.interfaces import VaultInterface
from app.utils.logger import get_logger, hash_id as _h

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constantes de chiffrement
# ---------------------------------------------------------------------------
_SALT_SIZE   = 16   # octets  -- sel PBKDF2 (aleatoire par entree)
_IV_SIZE     = 12   # octets  -- nonce GCM  (aleatoire par entree)
_TAG_SIZE    = 16   # octets  -- tag d'authentification GCM
_PBKDF2_ITER = 100_000
_KEY_SIZE    = 32   # octets  -- AES-256

VAULT_BLOB_VERSION = 0x02
VAULT_DEFAULT_KEY_VERSION = 0x01
VAULT_LEGACY_HEADER_DETECTION = "first byte != 0x02"

_BLOB_HEADER = _SALT_SIZE + _IV_SIZE + _TAG_SIZE  # 44 octets de header legacy
_BLOB_HEADER_V2 = 1 + 1 + _SALT_SIZE + _IV_SIZE + _TAG_SIZE


# ---------------------------------------------------------------------------
# Exceptions metier vault
# ---------------------------------------------------------------------------

class VaultUnavailableError(VaultError):
    """Leve quand le circuit breaker est OPEN (Redis definitivement indisponible)."""


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

@dataclass
class _CircuitBreaker:
    """
    Circuit breaker a 3 etats : CLOSED -> OPEN -> HALF_OPEN -> CLOSED.

    CLOSED     : fonctionnement normal
    OPEN       : Redis en echec, nouvelles requetes rejetees HTTP 503
    HALF_OPEN  : tentative de reconnexion apres HALF_OPEN_DELAY secondes

    Seuil      : FAILURE_THRESHOLD echecs consecutifs -> OPEN
    Delai      : HALF_OPEN_DELAY secondes avant tentative HALF_OPEN
    """

    FAILURE_THRESHOLD: int = 3
    HALF_OPEN_DELAY: float = 30.0  # secondes

    _failures: int = field(default=0, init=False)
    _state: str = field(default="CLOSED", init=False)
    _opened_at: float = field(default=0.0, init=False)

    @property
    def state(self) -> str:
        if self._state == "OPEN":
            if time.monotonic() - self._opened_at >= self.HALF_OPEN_DELAY:
                self._state = "HALF_OPEN"
        return self._state

    @property
    def is_open(self) -> bool:
        return self.state == "OPEN"

    def record_success(self) -> None:
        prev = self._state
        self._failures = 0
        self._state = "CLOSED"
        if prev != "CLOSED":
            logger.info(
                "circuit_breaker: Redis recupere | %s -> CLOSED | echecs_reinit=0",
                prev,
            )

    def record_failure(self) -> None:
        self._failures += 1
        was_open = self._state == "OPEN"
        if self._state == "HALF_OPEN" or self._failures >= self.FAILURE_THRESHOLD:
            if not was_open:
                logger.error(
                    "circuit_breaker: OUVERTURE | echecs_consecutifs=%d | "
                    "mode_degrade=HTTP_503 | retry_dans=%.0fs",
                    self._failures, self.HALF_OPEN_DELAY,
                )
            self._state = "OPEN"
            self._opened_at = time.monotonic()
        else:
            logger.warning(
                "circuit_breaker: echec Redis | count=%d/%d | state=CLOSED",
                self._failures, self.FAILURE_THRESHOLD,
            )


# ---------------------------------------------------------------------------
# Derivation de cle AES a partir de VAULT_ENCRYPTION_KEY + sel
# ---------------------------------------------------------------------------

def _derive_key(master_key_bytes: bytes, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_SIZE,
        salt=salt,
        iterations=_PBKDF2_ITER,
    )
    return kdf.derive(master_key_bytes)


def _derive_org_master(master_key: bytes, org_id: str, key_version: int) -> bytes:
    if not org_id:
        raise ValueError("org_id requis pour deriver la cle vault")
    if not 1 <= key_version <= 255:
        raise ValueError("key_version doit tenir sur 1 byte")
    kdf = HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_SIZE,
        salt=b"",
        info=b"vault:org:" + org_id.encode("utf-8") + b":v" + bytes([key_version]),
    )
    return kdf.derive(master_key)


# ---------------------------------------------------------------------------
# Chiffrement / dechiffrement AES-256-GCM
# ---------------------------------------------------------------------------

def _encrypt_v1(plaintext: bytes, master_key_bytes: bytes) -> bytes:
    salt = os.urandom(_SALT_SIZE)
    iv   = os.urandom(_IV_SIZE)
    key  = _derive_key(master_key_bytes, salt)
    aesgcm = AESGCM(key)
    ct_tag = aesgcm.encrypt(iv, plaintext, None)
    ciphertext = ct_tag[:-_TAG_SIZE]
    tag        = ct_tag[-_TAG_SIZE:]
    return salt + iv + tag + ciphertext


def _encrypt_v2(
    plaintext: bytes,
    master_key_bytes: bytes,
    org_id: str,
    key_version: int,
) -> bytes:
    salt = os.urandom(_SALT_SIZE)
    iv   = os.urandom(_IV_SIZE)
    org_master = _derive_org_master(master_key_bytes, org_id, key_version)
    key = _derive_key(org_master, salt)
    aesgcm = AESGCM(key)
    ct_tag = aesgcm.encrypt(iv, plaintext, None)
    ciphertext = ct_tag[:-_TAG_SIZE]
    tag        = ct_tag[-_TAG_SIZE:]
    return bytes([VAULT_BLOB_VERSION, key_version]) + salt + iv + tag + ciphertext


def _decrypt_v1(blob: bytes, master_key_bytes: bytes) -> bytes:
    if len(blob) < _BLOB_HEADER:
        raise ValueError(
            f"Blob trop court ({len(blob)} B, minimum {_BLOB_HEADER} B)"
        )
    salt       = blob[:_SALT_SIZE]
    iv         = blob[_SALT_SIZE:_SALT_SIZE + _IV_SIZE]
    tag        = blob[_SALT_SIZE + _IV_SIZE:_BLOB_HEADER]
    ciphertext = blob[_BLOB_HEADER:]
    key = _derive_key(master_key_bytes, salt)
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(iv, ciphertext + tag, None)


def _decrypt_v2(blob: bytes, master_key_bytes: bytes, org_id: str) -> bytes:
    if len(blob) < _BLOB_HEADER_V2:
        raise ValueError(
            f"Blob v2 trop court ({len(blob)} B, minimum {_BLOB_HEADER_V2} B)"
        )
    key_version = blob[1]
    salt_start = 2
    salt_end = salt_start + _SALT_SIZE
    iv_end = salt_end + _IV_SIZE
    tag_end = iv_end + _TAG_SIZE
    salt       = blob[salt_start:salt_end]
    iv         = blob[salt_end:iv_end]
    tag        = blob[iv_end:tag_end]
    ciphertext = blob[tag_end:]
    org_master = _derive_org_master(master_key_bytes, org_id, key_version)
    key = _derive_key(org_master, salt)
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(iv, ciphertext + tag, None)


def _master_key_bytes(master_key: bytes | str) -> bytes:
    if isinstance(master_key, str):
        return master_key.encode("utf-8")
    return master_key


def _is_v2_blob(blob: bytes) -> bool:
    return bool(blob) and blob[0] == VAULT_BLOB_VERSION


def encrypt_data(
    plaintext: bytes,
    master_key: bytes | str,
    org_id: str,
    key_version: int = VAULT_DEFAULT_KEY_VERSION,
) -> bytes:
    return _encrypt_v2(plaintext, _master_key_bytes(master_key), org_id, key_version)


def decrypt_data(blob: bytes, master_key: bytes | str, org_id: str) -> bytes:
    master_key_bytes = _master_key_bytes(master_key)
    if _is_v2_blob(blob):
        return _decrypt_v2(blob, master_key_bytes, org_id)
    return _decrypt_v1(blob, master_key_bytes)


def decrypt_and_migrate(
    blob: bytes,
    master_key: bytes | str,
    org_id: str,
) -> tuple[bytes, bytes]:
    master_key_bytes = _master_key_bytes(master_key)
    if _is_v2_blob(blob):
        return _decrypt_v2(blob, master_key_bytes, org_id), blob
    plaintext = _decrypt_v1(blob, master_key_bytes)
    return plaintext, encrypt_data(plaintext, master_key_bytes, org_id)


# ---------------------------------------------------------------------------
# Fallback memoire (si Redis est indisponible mais circuit CLOSED)
# ---------------------------------------------------------------------------

class _MemoryStore:
    """Stockage dict en memoire, thread-safe, avec expiration TTL.
    Utilise uniquement en dev/test quand Redis est down et circuit encore CLOSED.
    """

    def __init__(self) -> None:
        self._data:   Dict[str, bytes] = {}
        self._expiry: Dict[str, float] = {}
        self._lock = threading.Lock()

    def _purge_expired(self) -> None:
        now = time.monotonic()
        expired = [k for k, exp in self._expiry.items() if exp <= now]
        for k in expired:
            self._data.pop(k, None)
            self._expiry.pop(k, None)

    def set(self, key: str, value: bytes, ttl: int) -> None:
        with self._lock:
            self._purge_expired()
            self._data[key]   = value
            self._expiry[key] = time.monotonic() + ttl

    def get(self, key: str) -> Optional[bytes]:
        with self._lock:
            self._purge_expired()
            exp = self._expiry.get(key)
            if exp is None or time.monotonic() > exp:
                return None
            return self._data.get(key)

    def delete(self, key: str) -> bool:
        with self._lock:
            existed = key in self._data
            self._data.pop(key, None)
            self._expiry.pop(key, None)
            return existed

    def expire(self, key: str, ttl: int) -> bool:
        with self._lock:
            if key not in self._data:
                return False
            self._expiry[key] = time.monotonic() + ttl
            return True


# ---------------------------------------------------------------------------
# Classe principale — implementer VaultInterface
# ---------------------------------------------------------------------------

class Vault(VaultInterface):
    """
    Stockage chiffre AES-256-GCM des mappings PII, backend Redis async.

    Toutes les methodes publiques sont async.
    Circuit breaker : 3 echecs Redis -> OPEN -> HTTP 503 (jamais silencieux).
    Fallback memoire disponible uniquement en CLOSED avec < 3 echecs (dev/test).

    Deux API :
      - API Result (VaultInterface) : store/get/delete retournent Ok|Err
      - API legacy (backward-compat) : store_mapping/get_mapping/delete_mapping
        retournent bool/Optional[dict] pour les callers existants
    """

    def __init__(self) -> None:
        raw_key = settings.VAULT_ENCRYPTION_KEY
        self._master_key: bytes = raw_key.encode("utf-8")
        self._ttl: int = settings.REDIS_TTL_SECONDS

        self._redis: Optional[aioredis.Redis] = None
        self._redis_url: str = settings.REDIS_URL
        self._memory_store = _MemoryStore() if settings.ENVIRONMENT == "development" else None
        self._cb = _CircuitBreaker()

        logger.info(
            "Vault initialise | backend=redis | ttl=%ds | pbkdf2_iter=%d",
            self._ttl, _PBKDF2_ITER,
        )

    # ------------------------------------------------------------------
    # Connexion Redis lazy + circuit breaker
    # ------------------------------------------------------------------

    async def _get_redis(self) -> Optional[aioredis.Redis]:
        """
        Retourne le client Redis.
        Leve VaultUnavailableError si circuit OPEN.
        Retourne None uniquement en development si le fallback memoire est autorise.
        """
        if self._cb.is_open:
            raise VaultUnavailableError(
                "Redis indisponible (circuit ouvert). Reessayez dans "
                f"{self._cb.HALF_OPEN_DELAY:.0f}s."
            )

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
                self._cb.record_success()
                logger.info("Vault: connexion Redis etablie (%s)", self._redis_url)
            except Exception as exc:
                self._cb.record_failure()
                self._redis = None
                if self._cb.is_open:
                    raise VaultUnavailableError(
                        f"Redis indisponible apres {self._cb.FAILURE_THRESHOLD} echecs. "
                        "Circuit ouvert."
                    ) from exc
                if self._memory_store is not None:
                    logger.warning(
                        "Vault: Redis inaccessible (%s) | fallback memoire development",
                        exc,
                    )
                    return None
                raise VaultUnavailableError("Service temporairement indisponible") from exc

        return self._redis

    # ------------------------------------------------------------------
    # Chiffrement / dechiffrement (wrappers async)
    # ------------------------------------------------------------------

    def _encode(self, mapping: Dict[str, str], org_id: str) -> bytes:
        plaintext = json.dumps(mapping, ensure_ascii=False).encode("utf-8")
        return encrypt_data(plaintext, self._master_key, org_id)

    def _decode(self, blob: bytes, org_id: str) -> tuple[Dict[str, str], bytes]:
        plaintext, migrated_blob = decrypt_and_migrate(blob, self._master_key, org_id)
        return json.loads(plaintext.decode("utf-8")), migrated_blob

    # ------------------------------------------------------------------
    # VaultInterface — API Result (nouvelles routes / services)
    # ------------------------------------------------------------------

    async def store(self, user_id: str, session_id: str, mapping: Dict[str, str]) -> Result:
        """VaultInterface.store — retourne Ok(True) | Err(...)."""
        try:
            success = await self.store_mapping(session_id, mapping, user_id=user_id)
            if success:
                return ok(True)
            return err("Vault: echec du stockage", "VAULT_STORE_FAILED")
        except VaultUnavailableError as exc:
            return err(str(exc), "VAULT_UNAVAILABLE")
        except Exception as exc:
            return err(f"Vault: erreur inattendue: {type(exc).__name__}", "VAULT_ERROR")

    async def get(self, user_id: str, session_id: str) -> Result:
        """VaultInterface.get — retourne Ok(dict | None) | Err(...)."""
        try:
            mapping = await self.get_mapping(session_id, user_id=user_id)
            return ok(mapping)
        except VaultUnavailableError as exc:
            return err(str(exc), "VAULT_UNAVAILABLE")
        except Exception as exc:
            return err(f"Vault: erreur inattendue: {type(exc).__name__}", "VAULT_ERROR")

    async def delete(self, user_id: str, session_id: str) -> Result:
        """VaultInterface.delete — retourne Ok(bool) | Err(...)."""
        try:
            existed = await self.delete_mapping(session_id, user_id=user_id)
            return ok(existed)
        except VaultUnavailableError as exc:
            return err(str(exc), "VAULT_UNAVAILABLE")
        except Exception as exc:
            return err(f"Vault: erreur inattendue: {type(exc).__name__}", "VAULT_ERROR")

    # ------------------------------------------------------------------
    # API publique async (backward-compatible)
    # ------------------------------------------------------------------

    @staticmethod
    def new_session() -> str:
        return str(uuid.uuid4())

    async def store_mapping(
        self,
        session_id: str,
        mapping: Dict[str, str],
        user_id: str = "",
    ) -> bool:
        existing = await self.get_mapping(session_id, user_id=user_id) or {}
        existing.update(mapping)
        org_id = await self._org_id_for_user(user_id)

        redis_key = self._redis_key(session_id, user_id)
        blob = await asyncio.get_running_loop().run_in_executor(
            None, self._encode, existing, org_id
        )

        try:
            r = await self._get_redis()
            if r:
                await r.setex(redis_key, self._ttl, blob)
                self._cb.record_success()
            elif self._memory_store is not None:
                self._memory_store.set(redis_key, blob, self._ttl)
            else:
                raise VaultUnavailableError("Service temporairement indisponible")
            logger.debug(
                "vault.store | session=%s | tokens=%d",
                session_id, len(existing),
            )
            return True
        except VaultUnavailableError:
            raise
        except Exception as exc:
            self._cb.record_failure()
            logger.error(
                "vault.store ERREUR | session=%s | %s",
                session_id, type(exc).__name__,
            )
            return False

    async def get_mapping(
        self,
        session_id: str,
        user_id: str = "",
    ) -> Optional[Dict[str, str]]:
        redis_key = self._redis_key(session_id, user_id)
        blob: Optional[bytes] = None

        try:
            r = await self._get_redis()
            if r:
                blob = await r.get(redis_key)
                self._cb.record_success()
            elif self._memory_store is not None:
                blob = self._memory_store.get(redis_key)
            else:
                raise VaultUnavailableError("Service temporairement indisponible")
        except VaultUnavailableError:
            raise
        except Exception as exc:
            self._cb.record_failure()
            logger.error(
                "vault.get ERREUR | session=%s | %s",
                session_id, type(exc).__name__,
            )
            return None

        if blob is None:
            return None

        try:
            org_id = await self._org_id_for_user(user_id)
            mapping, migrated_blob = await asyncio.get_running_loop().run_in_executor(
                None, self._decode, blob, org_id
            )
            if migrated_blob != blob:
                r = await self._get_redis()
                if r:
                    await r.setex(redis_key, self._ttl, migrated_blob)
                elif self._memory_store is not None:
                    self._memory_store.set(redis_key, migrated_blob, self._ttl)
                logger.info(
                    "vault.migrated_v1_to_v2 | user=%s | org=%s",
                    _h(user_id), _h(org_id),
                )
            return mapping
        except Exception as exc:
            logger.error(
                "vault.decrypt ERREUR | session=%s | %s",
                session_id, type(exc).__name__,
            )
            return None

    async def delete_mapping(self, session_id: str, user_id: str = "") -> bool:
        redis_key = self._redis_key(session_id, user_id)

        try:
            r = await self._get_redis()
            if r:
                deleted = await r.delete(redis_key)
                existed = bool(deleted)
                self._cb.record_success()
            elif self._memory_store is not None:
                existed = self._memory_store.delete(redis_key)
            else:
                raise VaultUnavailableError("Service temporairement indisponible")

            if existed:
                logger.info(
                    "vault.delete | session=%s | droit_effacement=ok",
                    session_id,
                )
            return existed
        except VaultUnavailableError:
            raise
        except Exception as exc:
            self._cb.record_failure()
            logger.error(
                "vault.delete ERREUR | session=%s | %s",
                session_id, type(exc).__name__,
            )
            return False

    async def extend_ttl(self, session_id: str, seconds: int, user_id: str = "") -> bool:
        if seconds <= 0:
            raise ValueError(f"extend_ttl: seconds doit etre > 0 (recu: {seconds})")

        redis_key = self._redis_key(session_id, user_id)

        try:
            r = await self._get_redis()
            if r:
                result = await r.expire(redis_key, seconds)
                ok_ = bool(result)
                self._cb.record_success()
            elif self._memory_store is not None:
                ok_ = self._memory_store.expire(redis_key, seconds)
            else:
                raise VaultUnavailableError("Service temporairement indisponible")

            if ok_:
                logger.debug(
                    "vault.extend_ttl | session=%s | nouveau_ttl=%ds",
                    session_id, seconds,
                )
            return ok_
        except VaultUnavailableError:
            raise
        except Exception as exc:
            self._cb.record_failure()
            logger.error(
                "vault.extend_ttl ERREUR | session=%s | %s",
                session_id, type(exc).__name__,
            )
            return False

    async def restore(self, session_id: str, text: str, user_id: str = "") -> str:
        """Remplace les tokens dans text par leurs valeurs originales (backward compat).

        Signature conservee pour la compatibilite avec les routes existantes.
        Implémente VaultInterface.restore via la meme methode.
        """
        mapping = await self.get_mapping(session_id, user_id=user_id)
        if not mapping:
            return text
        for token, original in mapping.items():
            text = re.sub(re.escape(token), lambda _m, _o=original: _o, text, flags=re.IGNORECASE)
        return text

    # ------------------------------------------------------------------
    # Helpers internes
    # ------------------------------------------------------------------

    @staticmethod
    def _redis_key(session_id: str, user_id: str = "") -> str:
        if user_id:
            return f"vault:{user_id}:{session_id}"
        return f"vault:{session_id}"

    async def _org_id_for_user(self, user_id: str) -> str:
        if not user_id:
            raise ValueError("user_id requis pour resoudre org_id vault")
        r = await self._get_redis()
        raw = await r.get(f"user:{user_id}") if r else None
        if raw is None:
            raise ValueError("org_id introuvable pour user vault")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        org_id = str(data.get("org_id") or "").strip()
        if not org_id:
            raise ValueError("org_id vide pour user vault")
        return org_id

    @property
    def circuit_state(self) -> str:
        """Expose l'etat du circuit breaker pour /health."""
        return self._cb.state

    async def health_check(self) -> Dict[str, object]:
        r = await self._get_redis() if not self._cb.is_open else None
        return {
            "backend": "redis" if r else ("memory_fallback" if self._memory_store is not None else "unavailable"),
            "redis_available": not self._cb.is_open,
            "circuit_state": self._cb.state,
            "ttl_seconds": self._ttl,
        }
