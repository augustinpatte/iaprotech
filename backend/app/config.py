"""
Configuration management via pydantic-settings.

Toutes les valeurs sont surchargeable via variables d'environnement ou
un fichier .env à la racine du projet.

Utilisation :
    from app.config import settings
    print(settings.REDIS_URL)

Validation au démarrage :
    from app.config import validate_config
    validate_config()   # appelé dans main.py → startup_event
"""

import re
from typing import Any, Dict, List, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


SUPPORTED_FILE_FORMATS: Dict[str, Dict[str, Any]] = {
    "pdf": {"preview": True, "pseudonymize": True, "chat_context": True, "export": "pdf", "excel_controls": False},
    "docx": {"preview": True, "pseudonymize": True, "chat_context": True, "export": "docx", "excel_controls": False},
    "doc": {"preview": True, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "odt": {"preview": True, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "xlsx": {"preview": True, "pseudonymize": True, "chat_context": True, "export": "xlsx", "excel_controls": True},
    "xls": {"preview": True, "pseudonymize": True, "chat_context": True, "export": "xlsx", "excel_controls": False},
    "ods": {"preview": True, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "pptx": {"preview": True, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "ppt": {"preview": True, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "odp": {"preview": True, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "txt": {"preview": True, "pseudonymize": True, "chat_context": True, "export": "txt", "excel_controls": False},
    "csv": {"preview": True, "pseudonymize": True, "chat_context": True, "export": "csv", "excel_controls": False},
    "tsv": {"preview": True, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "rtf": {"preview": True, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "md": {"preview": True, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "jpg": {"preview": False, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "jpeg": {"preview": False, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "png": {"preview": False, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "gif": {"preview": False, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "webp": {"preview": False, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "bmp": {"preview": False, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "tiff": {"preview": False, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "tif": {"preview": False, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "eml": {"preview": True, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "msg": {"preview": True, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "vtt": {"preview": True, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "srt": {"preview": True, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "html": {"preview": True, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "htm": {"preview": True, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "json": {"preview": True, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "xml": {"preview": True, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "yaml": {"preview": True, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
    "yml": {"preview": True, "pseudonymize": True, "chat_context": True, "export": False, "excel_controls": False},
}


# ---------------------------------------------------------------------------
# Modèle de configuration principal
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )
    OCR_ENABLED: bool = Field(
        default=True,
        description="Active l'OCR image si Tesseract est disponible au demarrage.",
    )
    VAULT_TTL_SECONDS: int = Field(
        default=3600,
        ge=60,
        description="Duree de retention du vault en secondes.",
    )

    # -----------------------------------------------------------------------
    # Général
    # -----------------------------------------------------------------------
    ENVIRONMENT: Literal["development", "production"] = Field(
        default="development",
        description="Environnement d'execution. Controle certains comportements fail-closed.",
    )
    DEBUG: bool = False
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    ALLOWED_ORIGINS: List[str] = ["http://localhost:5173", "http://localhost:3000"]

    # -----------------------------------------------------------------------
    # Authentification / JWT
    # -----------------------------------------------------------------------
    SECRET_KEY: str = Field(
        ...,
        min_length=32,
        description="Clé secrète JWT — minimum 32 caractères, générée aléatoirement.",
    )
    ALGORITHM: str = "HS256"
    TOKEN_EXPIRE_MINUTES: int = Field(
        default=60,
        ge=5,
        le=1440,
        description="Durée de vie des tokens JWT en minutes (5 min – 24 h).",
    )

    # Lire X-Forwarded-For pour l'IP reelle.
    # True SEULEMENT derriere un reverse proxy de confiance (nginx, Traefik...).
    # En acces direct Internet, False empeche le spoofing du rate limit.
    TRUST_PROXY_HEADERS: bool = Field(
        default=False,
        description="Utiliser X-Forwarded-For pour l'IP reelle du client.",
    )

    # Activer la route POST /api/auth/bootstrap.
    # Mettre a false apres creation du premier admin pour fermer ce chemin.
    BOOTSTRAP_ENABLED: bool = Field(
        default=False,
        description=(
            "Activer UNIQUEMENT pour le premier deploiement afin de creer le compte admin. "
            "Remettre a false immediatement apres. "
            "En production, cette valeur DOIT etre false."
        ),
    )
    DEV_AUTO_CREATE_ADMIN: bool = Field(
        default=False,
        description=(
            "Mode developpement uniquement : cree ou reinitialise un compte admin "
            "au demarrage pour faciliter les tests locaux."
        ),
    )
    DEV_ADMIN_USERNAME: str = Field(
        default="",
        description="Nom du compte admin de developpement cree automatiquement.",
    )
    DEV_ADMIN_PASSWORD: str = Field(
        default="",
        description="Mot de passe du compte admin de developpement.",
    )

    # -----------------------------------------------------------------------
    # Vault — chiffrement AES-256-GCM avec PBKDF2-SHA256
    # -----------------------------------------------------------------------
    VAULT_ENCRYPTION_KEY: str = Field(
        ...,
        min_length=32,
        description=(
            "Cle maitre du vault. Utilisee comme entree PBKDF2-SHA256 pour "
            "deriver les cles AES-256-GCM qui chiffrent les donnees PII dans "
            "Redis. Doit faire au moins 32 caracteres avec entropie elevee. "
            "Generer avec: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        ),
    )

    # -----------------------------------------------------------------------
    # Redis
    # -----------------------------------------------------------------------
    REDIS_URL: str = Field(
        default="redis://localhost:6379/0",
        description="URL de connexion Redis. Format : redis://[:password@]host:port/db",
    )
    REDIS_TTL_SECONDS: int = Field(
        default=3600,
        ge=60,
        description="Durée de vie (TTL) des mappings PII chiffrés dans Redis (en secondes).",
    )
    PROJECT_TTL_DAYS: int = Field(
        default=30,
        ge=1,
        le=365,
        description=(
            "Duree de vie (inactivite) des projets avant archivage automatique "
            "et purge du vault associe (jours). "
            "Doit etre superieur a REDIS_TTL_SECONDS / 86400 pour eviter "
            "des archivages avant expiration naturelle du vault."
        ),
    )

    # -----------------------------------------------------------------------
    # Fournisseurs LLM
    # -----------------------------------------------------------------------
    LLM_PROVIDER: Literal["anthropic", "openai", "google", "mistral"] = Field(
        default="anthropic",
        description="Fournisseur LLM par défaut (utilisé quand ROUTER_ENABLED=false).",
    )
    ANTHROPIC_API_KEY: str = Field(
        default="",
        description="Clé API Anthropic. Obligatoire si LLM_PROVIDER='anthropic'.",
    )
    OPENAI_API_KEY: str = Field(
        default="",
        description="Clé API OpenAI. Obligatoire si LLM_PROVIDER='openai'.",
    )
    GEMINI_API_KEY: str = Field(
        default="",
        description="Clé API Google AI Studio (Gemini). Obligatoire si LLM_PROVIDER='google'.",
    )
    MISTRAL_API_KEY: str = Field(
        default="",
        description="Clé API Mistral. Obligatoire si LLM_PROVIDER='mistral'.",
    )
    DEFAULT_MODEL: str = Field(
        default="claude-sonnet-4-5",
        description="Identifiant du modèle utilisé par défaut (fallback si router désactivé).",
    )

    # -----------------------------------------------------------------------
    # LLM Router
    # -----------------------------------------------------------------------
    ROUTER_ENABLED: bool = Field(
        default=True,
        description=(
            "Active le routeur intelligent multi-provider. "
            "Quand True, analyze_request() + select_model() sélectionnent automatiquement "
            "le meilleur modèle selon la complexité et le type de tâche."
        ),
    )
    DEFAULT_COMPLEXITY_ANALYZER: str = Field(
        default="claude-haiku-4-5",
        description=(
            "Modèle utilisé pour analyser la complexité des requêtes. "
            "Doit être une clé du MODEL_REGISTRY dans router.py. "
            "Recommandé : modèle rapide et bon marché (haiku, flash, mini)."
        ),
    )

    # -----------------------------------------------------------------------
    # Détection de langue et redaction
    # -----------------------------------------------------------------------
    SUPPORTED_LANGUAGES: List[str] = Field(
        default=["fr", "en", "de", "es", "it", "pt", "nl"],
        description="Codes ISO 639-1 des langues supportées pour l'analyse PII.",
    )
    REDACTION_ENGINE: Literal["presidio", "local"] = "presidio"
    SPACY_MODEL: str = Field(
        default="fr_core_news_lg",
        description="Modèle spaCy principal (langue par défaut = français).",
    )
    REDACTION_ENTITIES: List[str] = Field(
        default=[
            "PERSON",
            "EMAIL_ADDRESS",
            "PHONE_NUMBER",
            "IBAN_CODE",
            "CREDIT_CARD",
            "LOCATION",
            "DATE_TIME",
            "NRP",
            "IP_ADDRESS",
            "URL",
            "MEDICAL_LICENSE",
            "FR_NIF",
        ],
        description="Types d'entités PII à détecter et masquer.",
    )

    # -----------------------------------------------------------------------
    # Limitation de débit
    # -----------------------------------------------------------------------
    # -----------------------------------------------------------------------
    # Plan d'usage
    # -----------------------------------------------------------------------
    PLAN_NAME: str = Field(
        default="starter",
        description="Nom du plan souscrit (starter, pro, enterprise...).",
    )
    PLAN_MONTHLY_TOKENS: int = Field(
        default=50000,
        ge=0,
        description="Quota mensuel de tokens (input + output cumules).",
    )
    PLAN_MONTHLY_REQUESTS: int = Field(
        default=500,
        ge=0,
        description="Quota mensuel de requetes LLM.",
    )
    PLAN_MONTHLY_COST_USD: float = Field(
        default=10.0,
        ge=0.0,
        description="Budget mensuel alloue en USD pour le dashboard de cout.",
    )
    PLAN_LIMITS: Dict[str, Any] = Field(
        default_factory=dict,
        description="Overrides de limites de plan charges depuis l'environnement si necessaire.",
    )

    # -----------------------------------------------------------------------
    # Limitation de debit
    # -----------------------------------------------------------------------
    MAX_REQUESTS_PER_MINUTE: int = Field(
        default=60,
        ge=1,
        le=1000,
        description="Nombre maximal de requêtes par minute par utilisateur.",
    )
    RATE_LIMIT_PROXY: str = Field(
        default="20/minute",
        description="Limite slowapi appliquée aux routes /proxy (ex: '20/minute').",
    )

    # -----------------------------------------------------------------------
    # Traitement de fichiers
    # -----------------------------------------------------------------------
    MAX_FILE_SIZE_MB: int = Field(
        default=20,
        ge=1,
        le=50,
        description="Taille maximale autorisée pour les fichiers uploadés (en Mo).",
    )
    PROCESSING_COST_PER_MB: float = Field(
        default=0.001,
        ge=0.0,
        description="Cout indicatif en USD par Mo pour le traitement local.",
    )
    ALLOWED_FILE_TYPES: List[str] = Field(
        default_factory=lambda: [
            "pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt",
            "odt", "ods", "odp",
            "txt", "csv", "tsv", "rtf", "md",
            "jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "tif",
            "eml", "msg",
            "vtt", "srt",
            "html", "htm",
            "json", "xml", "yaml", "yml",
        ],
        description="Extensions de fichiers acceptées pour l'upload.",
    )

    # -----------------------------------------------------------------------
    # Validateurs Pydantic
    # -----------------------------------------------------------------------

    @field_validator("SECRET_KEY")
    @classmethod
    def secret_key_strength(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError(
                f"SECRET_KEY trop courte ({len(v)} cars). "
                "Minimum 32 caractères. "
                "Générer : python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        if v in ("CHANGE_ME_STRONG_RANDOM_SECRET", "changeme", "secret"):
            raise ValueError(
                "SECRET_KEY invalide : utilisez une valeur aléatoirement générée."
            )
        return v

    @field_validator("VAULT_ENCRYPTION_KEY")
    @classmethod
    def _validate_vault_key_entropy(cls, v: str) -> str:
        if v in ("CHANGE_ME_FERNET_KEY", "", "changeme"):
            raise ValueError(
                "VAULT_ENCRYPTION_KEY non configuree. Generer avec: "
                "python -c \"import secrets; print(secrets.token_urlsafe(32))\""
            )
        if len(v) < 32:
            raise ValueError(
                "VAULT_ENCRYPTION_KEY doit faire au moins 32 caracteres"
            )
        if v.lower() in {"password", "secret", "test", "admin", "default"}:
            raise ValueError(
                "VAULT_ENCRYPTION_KEY trop faible (valeur triviale detectee)"
            )
        if len(set(v)) < 10:
            raise ValueError(
                "VAULT_ENCRYPTION_KEY a trop peu de caracteres uniques "
                "(entropie insuffisante)"
            )
        return v

    @field_validator("REDIS_URL")
    @classmethod
    def redis_url_format(cls, v: str) -> str:
        pattern = r"^rediss?://"
        if not re.match(pattern, v):
            raise ValueError(
                f"REDIS_URL invalide : '{v}'. "
                "Format attendu : redis://[:password@]host:port/db"
            )
        return v

    @field_validator("SUPPORTED_LANGUAGES")
    @classmethod
    def languages_not_empty(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("SUPPORTED_LANGUAGES ne peut pas être vide.")
        valid = {"fr", "en", "de", "es", "it", "pt", "nl", "zh", "ja", "ar"}
        unknown = set(v) - valid
        if unknown:
            raise ValueError(
                f"Langues non supportées par les modèles spaCy disponibles : {unknown}"
            )
        return v

    @model_validator(mode="after")
    def llm_api_key_present(self) -> "Settings":
        """Vérifie qu'une clé API est fournie pour le fournisseur LLM actif (fallback)."""
        _required: Dict[str, str] = {
            "anthropic": self.ANTHROPIC_API_KEY,
            "openai":    self.OPENAI_API_KEY,
            "google":    self.GEMINI_API_KEY,
            "mistral":   self.MISTRAL_API_KEY,
        }
        key = _required.get(self.LLM_PROVIDER, "")
        if not key:
            provider_env = {
                "anthropic": "ANTHROPIC_API_KEY",
                "openai":    "OPENAI_API_KEY",
                "google":    "GEMINI_API_KEY",
                "mistral":   "MISTRAL_API_KEY",
            }[self.LLM_PROVIDER]
            raise ValueError(
                f"{provider_env} est obligatoire quand LLM_PROVIDER='{self.LLM_PROVIDER}'."
            )
        return self


# ---------------------------------------------------------------------------
# Singleton — importé par les autres modules
# ---------------------------------------------------------------------------

settings = Settings()


# ---------------------------------------------------------------------------
# validate_config() — appelée explicitement au démarrage de l'application
# ---------------------------------------------------------------------------

def validate_config() -> None:
    """
    Contrôles de cohérence complémentaires exécutés après l'instanciation
    de Settings (les validateurs Pydantic s'exécutent à l'instanciation).

    Lève une RuntimeError avec un message explicite si une condition critique
    n'est pas remplie.
    """
    if settings.ENVIRONMENT == "production" and settings.BOOTSTRAP_ENABLED:
        raise RuntimeError(
            "SECURITY: BOOTSTRAP_ENABLED must be False in production. "
            "Set BOOTSTRAP_ENABLED=false in your .env after creating the first admin."
        )

    errors: list[str] = []

    # Clés secrets non remplacées
    placeholders = {
        "SECRET_KEY": ("CHANGE_ME", "secret", "changeme"),
        "VAULT_ENCRYPTION_KEY": ("CHANGE_ME", "changeme"),
    }
    for field_name, bad_values in placeholders.items():
        value = getattr(settings, field_name, "")
        if any(bad in value for bad in bad_values):
            errors.append(
                f"  • {field_name} contient une valeur placeholder non remplacée."
            )

    # Longueur minimale SECRET_KEY (défense en profondeur)
    if len(settings.SECRET_KEY) < 32:
        errors.append(
            f"  • SECRET_KEY trop courte ({len(settings.SECRET_KEY)} cars, minimum 32)."
        )

    # Clé API LLM cohérente avec le fournisseur (fallback quand router désactivé)
    provider_key_map = {
        "anthropic": ("ANTHROPIC_API_KEY", settings.ANTHROPIC_API_KEY),
        "openai":    ("OPENAI_API_KEY",    settings.OPENAI_API_KEY),
        "google":    ("GEMINI_API_KEY",    settings.GEMINI_API_KEY),
        "mistral":   ("MISTRAL_API_KEY",   settings.MISTRAL_API_KEY),
    }
    key_name, key_value = provider_key_map.get(settings.LLM_PROVIDER, ("", ""))
    if not key_value and key_name:
        errors.append(f"  • {key_name} manquante (LLM_PROVIDER='{settings.LLM_PROVIDER}').")

    # Format des clés API (validation de préfixe basique)
    if settings.ANTHROPIC_API_KEY and not settings.ANTHROPIC_API_KEY.startswith("sk-ant-"):
        errors.append(
            "  • ANTHROPIC_API_KEY semble invalide (doit commencer par 'sk-ant-')."
        )
    if settings.OPENAI_API_KEY and not settings.OPENAI_API_KEY.startswith("sk-"):
        errors.append(
            "  • OPENAI_API_KEY semble invalide (doit commencer par 'sk-')."
        )

    # Redis URL accessible (test de format uniquement — pas de connexion ici)
    if not re.match(r"^rediss?://", settings.REDIS_URL):
        errors.append(f"  • REDIS_URL invalide : '{settings.REDIS_URL}'.")

    # MAX_FILE_SIZE cohérent
    if settings.MAX_FILE_SIZE_MB < 1 or settings.MAX_FILE_SIZE_MB > 50:
        errors.append(
            f"  • MAX_FILE_SIZE_MB={settings.MAX_FILE_SIZE_MB} hors plage [1, 50]."
        )

    # TOKEN_EXPIRE_MINUTES cohérent
    if settings.TOKEN_EXPIRE_MINUTES < 5:
        errors.append(
            f"  • TOKEN_EXPIRE_MINUTES={settings.TOKEN_EXPIRE_MINUTES} trop faible (minimum 5)."
        )

    if errors:
        msg = "Erreurs de configuration détectées :\n" + "\n".join(errors)
        raise RuntimeError(msg)

    # Log de confirmation (import tardif pour éviter les imports circulaires)
    try:
        from app.utils.logger import get_logger
        logger = get_logger(__name__)
        logger.info(
            "Configuration validée — provider=%s | langues=%s | max_file=%dMo | ttl=%ds",
            settings.LLM_PROVIDER,
            settings.SUPPORTED_LANGUAGES,
            settings.MAX_FILE_SIZE_MB,
            settings.REDIS_TTL_SECONDS,
        )
    except ImportError:
        pass
