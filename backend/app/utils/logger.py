"""
Logger RGPD zero-content — Privacy Proxy.

Garanties RGPD :
  • Aucun contenu personnel n'est jamais loggé (pas de texte, pas d'entités).
  • Les identifiants session_id et user_id sont hashés SHA-256 avant écriture
    (irreversibles — conformément à l'Art. 5(1)(f) du RGPD).
  • Les champs loggés sont limités à : timestamp, level, action, session_id_hash,
    user_id_hash, path, duration_ms et des métadonnées non-sensibles (extra).

Format de sortie :
  • Production (DEBUG=False) : JSON structuré ligne par ligne.
  • Développement (DEBUG=True) : format lisible en couleur.

Rotation :
  • Fichier : logs/privacy_proxy.log (RotatingFileHandler)
  • Taille max : 10 Mo par fichier, 5 backups conservés.

API publique :
  • get_logger(name)           → logging.Logger  (usage standard dans les modules)
  • log_action(action, ...)   → None             (entrée d'audit structurée RGPD)
"""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import sys
import time
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

_LOG_DIR  = Path("/app/logs")
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_FILE = _LOG_DIR / "privacy_proxy.log"
_MAX_BYTES   = 10 * 1024 * 1024   # 10 Mo par fichier
_BACKUP_COUNT = 5

_LEVELS: dict[str, int] = {
    "DEBUG":    logging.DEBUG,
    "INFO":     logging.INFO,
    "WARNING":  logging.WARNING,
    "ERROR":    logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

_configured = False


# ---------------------------------------------------------------------------
# Hashing RGPD — SHA-256 tronqué à 16 hex (non réversible)
# ---------------------------------------------------------------------------

def _hash_id(value: Optional[str]) -> Optional[str]:  # noqa: N802
    """
    Retourne les 16 premiers caractères du digest SHA-256 de *value*.
    Retourne None si *value* est vide ou None.
    Jamais la valeur originale n'est loggée.
    """
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


# Public aliases — importable by other modules as:
#   from app.utils.logger import hash_id
#   from app.utils.logger import h
hash_id = _hash_id
h = _hash_id  # short alias for inline log formatting


# ---------------------------------------------------------------------------
# Formateurs
# ---------------------------------------------------------------------------

class _JsonFormatter(logging.Formatter):
    """
    Formateur JSON structuré.
    Chaque log record est sérialisé en une ligne JSON.
    Les champs supplémentaires (action, session_id_hash, …) sont fusionnés
    si présents dans le record (injectés par log_action).
    """

    _EXTRA_FIELDS = (
        "action", "session_id_hash", "user_id_hash",
        "path", "duration_ms", "extra",
    )

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level":     record.levelname,
            "logger":    record.name,
            "message":   record.getMessage(),
        }
        for field in self._EXTRA_FIELDS:
            val = getattr(record, field, None)
            if val is not None:
                entry[field] = val
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


_DEV_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s"
)


# ---------------------------------------------------------------------------
# Configuration (singleton)
# ---------------------------------------------------------------------------

def _configure_root_logger(debug: bool = False, log_level: str = "INFO") -> None:
    """
    Configure le logger racine une seule fois.
    Ajoute :
      • StreamHandler (stdout) — toujours présent
      • RotatingFileHandler  — en production (DEBUG=False)
    """
    global _configured
    if _configured:
        return
    _configured = True

    level = logging.DEBUG if debug else _LEVELS.get(log_level.upper(), logging.INFO)
    root  = logging.getLogger()
    root.setLevel(level)

    # ── Handler console ──────────────────────────────────────────────────────
    stream_handler = logging.StreamHandler(sys.stdout)
    if debug:
        stream_handler.setFormatter(
            logging.Formatter(_DEV_FORMAT, datefmt="%Y-%m-%dT%H:%M:%S")
        )
    else:
        stream_handler.setFormatter(_JsonFormatter())
    root.addHandler(stream_handler)

    # ── Handler fichier avec rotation (production uniquement) ─────────────
    if not debug:
        try:
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                filename=_LOG_FILE,
                maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setFormatter(_JsonFormatter())
            root.addHandler(file_handler)
        except OSError:
            # Pas de droits d'écriture (ex: conteneur read-only) — on continue sans
            root.warning("Impossible d'ouvrir le fichier de log : %s", _LOG_FILE)

    # ── Silence des loggers bruités ──────────────────────────────────────────
    for noisy in ("uvicorn.access", "httpx", "httpcore", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# API publique — get_logger
# ---------------------------------------------------------------------------

def get_logger(name: Optional[str] = None) -> logging.Logger:
    """
    Retourne un logger nommé après avoir initialisé la configuration racine.
    Les paramètres DEBUG / LOG_LEVEL sont lus depuis les settings (lazy import
    pour éviter les imports circulaires au démarrage).

    Usage :
        logger = get_logger(__name__)
        logger.info("Démarrage du module")
    """
    try:
        from app.config import settings  # import lazy
        debug     = settings.DEBUG
        log_level = "DEBUG" if debug else getattr(settings, "LOG_LEVEL", "INFO")
    except Exception:
        debug     = False
        log_level = "INFO"

    _configure_root_logger(debug=debug, log_level=log_level)
    return logging.getLogger(name or "privacy_proxy")


# ---------------------------------------------------------------------------
# API publique — log_action  (entrées d'audit RGPD)
# ---------------------------------------------------------------------------

_audit_logger = logging.getLogger("privacy_proxy.audit")


def log_action(
    action: str,
    *,
    session_id: Optional[str] = None,
    user_id:    Optional[str] = None,
    path:       Optional[str] = None,
    duration_ms: Optional[float] = None,
    extra:      Optional[dict[str, Any]] = None,
    level: int = logging.INFO,
) -> None:
    """
    Enregistre une entrée d'audit structurée sans aucun contenu personnel.

    Args:
        action      : identifiant de l'action (ex: "chat.request", "upload.done")
        session_id  : ID de session — hashé SHA-256 avant écriture (jamais en clair)
        user_id     : ID utilisateur — hashé SHA-256 avant écriture (jamais en clair)
        path        : chemin HTTP (ex: "/api/chat/stream")
        duration_ms : durée de traitement en millisecondes
        extra       : dict de métadonnées non-sensibles (ex: {"chunks": 3, "entities": 7})
        level       : niveau de log (logging.INFO par défaut)

    Niveaux recommandés :
        logging.INFO    — requêtes normales
        logging.WARNING — rate limit atteint, validations échouées
        logging.ERROR   — exceptions, erreurs serveur

    Garanties RGPD :
        • session_id et user_id sont irréversiblement hashés (SHA-256 16 hex)
        • Le paramètre *extra* NE DOIT contenir aucun texte ou PII —
          uniquement des compteurs, flags, codes d'erreur, durées.
    """
    # Initialiser la configuration si get_logger n'a pas encore été appelé
    _configure_root_logger()

    record = _audit_logger.makeRecord(
        name    = _audit_logger.name,
        level   = level,
        fn      = "",
        lno     = 0,
        msg     = action,
        args    = (),
        exc_info= None,
    )

    # Champs structurés — jamais de valeurs en clair pour session/user
    record.action          = action
    record.session_id_hash = _hash_id(session_id)
    record.user_id_hash    = _hash_id(user_id)
    record.path            = path
    record.duration_ms     = round(duration_ms, 2) if duration_ms is not None else None
    record.extra           = extra or {}

    _audit_logger.handle(record)


# ---------------------------------------------------------------------------
# Utilitaire — mesure de durée
# ---------------------------------------------------------------------------

class Timer:
    """
    Chronomètre contextuel pour mesurer la durée d'une opération.

    Usage :
        with Timer() as t:
            await do_work()
        log_action("work.done", duration_ms=t.elapsed_ms)
    """

    def __enter__(self) -> "Timer":
        self._start = time.monotonic()
        self._end: Optional[float] = None
        return self

    def __exit__(self, *_: Any) -> None:
        self._end = time.monotonic()

    @property
    def elapsed_ms(self) -> float:
        end = self._end if self._end is not None else time.monotonic()
        return (end - self._start) * 1000
