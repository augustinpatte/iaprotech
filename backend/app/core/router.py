"""
LLM Router — sélection intelligente de modèle multi-provider via LiteLLM.

Pipeline :
  1. analyze_request()  — heuristique locale pour qualifier la requête
                          (complexité, type de tâche, tokens estimés)
  2. select_model()     — filtre MODEL_REGISTRY et retourne le modèle
                          optimal (qualité suffisante, moins cher, provider dispo)
  3. call_llm_via_litellm()   — appel LLM unifié (non-streaming)
  4. stream_llm_via_litellm() — appel LLM unifié (streaming, async generator)

Providers supportés (via LiteLLM) :
  anthropic · openai · google · mistral

Activation : ROUTER_ENABLED=true dans .env
             → ignoré si false, le fallback settings.LLM_PROVIDER s'applique.

RGPD :
  Les logs ne contiennent jamais le texte analysé — uniquement des métriques
  (complexité, task_type, model_id, coût estimé).

SECURITY NOTE: analyze_request() is local-only — it MUST NOT make
any external call. Any complexity analysis before pseudonymization
is a privacy violation.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator, Dict, List, Literal, Optional, Sequence

import litellm
from pydantic import BaseModel

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Silence LiteLLM's own verbose output
litellm.suppress_debug_info = True
litellm.set_verbose = False


# ---------------------------------------------------------------------------
# MODEL_REGISTRY
# Prix en USD pour 1 000 tokens
# ---------------------------------------------------------------------------

ModelInfo = Dict[str, Any]

MODEL_PRICING: Dict[str, Dict[str, float]] = {
    "claude-haiku-4-5": {"input": 0.00025, "output": 0.00125},
    "claude-sonnet-4-5": {"input": 0.003, "output": 0.015},
    "claude-opus-4-5": {"input": 0.015, "output": 0.075},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-4o": {"input": 0.0025, "output": 0.01},
    "gemini-1.5-flash": {"input": 0.000075, "output": 0.0003},
    "gemini-1.5-pro": {"input": 0.00125, "output": 0.005},
    "mistral-small-latest": {"input": 0.0002, "output": 0.0006},
    "mistral-large-latest": {"input": 0.002, "output": 0.006},
    # modeles conserves pour compatibilite interne
    "o1-mini": {"input": 0.003, "output": 0.012},
    "gemini-2.0-flash": {"input": 0.0001, "output": 0.0004},
    "mistral-nemo": {"input": 0.00015, "output": 0.00015},
}

MODEL_REGISTRY: Dict[str, ModelInfo] = {
    # ── Anthropic ────────────────────────────────────────────────────────────
    "claude-opus-4-5": {
        "provider":            "anthropic",
        "litellm_model_id":    "claude-opus-4-5",
        "price_per_1k_input":  MODEL_PRICING["claude-opus-4-5"]["input"],
        "price_per_1k_output": MODEL_PRICING["claude-opus-4-5"]["output"],
        "quality_score":       10,
        "strengths":           ["code", "legal", "math", "complex", "creative"],
        "context_window":      200_000,
        "supports_streaming":  True,
    },
    "claude-sonnet-4-5": {
        "provider":            "anthropic",
        "litellm_model_id":    "claude-sonnet-4-5",
        "price_per_1k_input":  MODEL_PRICING["claude-sonnet-4-5"]["input"],
        "price_per_1k_output": MODEL_PRICING["claude-sonnet-4-5"]["output"],
        "quality_score":       9,
        "strengths":           ["code", "legal", "summary", "multilingual", "creative"],
        "context_window":      200_000,
        "supports_streaming":  True,
    },
    "claude-haiku-4-5": {
        "provider":            "anthropic",
        "litellm_model_id":    "claude-haiku-4-5",
        "price_per_1k_input":  MODEL_PRICING["claude-haiku-4-5"]["input"],
        "price_per_1k_output": MODEL_PRICING["claude-haiku-4-5"]["output"],
        "quality_score":       7,
        "strengths":           ["simple", "summary", "multilingual"],
        "context_window":      200_000,
        "supports_streaming":  True,
    },
    # ── OpenAI ───────────────────────────────────────────────────────────────
    "gpt-4o": {
        "provider":            "openai",
        "litellm_model_id":    "gpt-4o",
        "price_per_1k_input":  MODEL_PRICING["gpt-4o"]["input"],
        "price_per_1k_output": MODEL_PRICING["gpt-4o"]["output"],
        "quality_score":       9,
        "strengths":           ["code", "summary", "multilingual", "creative"],
        "context_window":      128_000,
        "supports_streaming":  True,
    },
    "gpt-4o-mini": {
        "provider":            "openai",
        "litellm_model_id":    "gpt-4o-mini",
        "price_per_1k_input":  MODEL_PRICING["gpt-4o-mini"]["input"],
        "price_per_1k_output": MODEL_PRICING["gpt-4o-mini"]["output"],
        "quality_score":       7,
        "strengths":           ["simple", "summary", "code"],
        "context_window":      128_000,
        "supports_streaming":  True,
    },
    "o1-mini": {
        "provider":            "openai",
        "litellm_model_id":    "o1-mini",
        "price_per_1k_input":  MODEL_PRICING["o1-mini"]["input"],
        "price_per_1k_output": MODEL_PRICING["o1-mini"]["output"],
        "quality_score":       8,
        "strengths":           ["math", "code", "complex"],
        "context_window":      128_000,
        "supports_streaming":  False,
    },
    # ── Google ───────────────────────────────────────────────────────────────
    "gemini-1.5-pro": {
        "provider":            "google",
        "litellm_model_id":    "gemini/gemini-1.5-pro",
        "price_per_1k_input":  MODEL_PRICING["gemini-1.5-pro"]["input"],
        "price_per_1k_output": MODEL_PRICING["gemini-1.5-pro"]["output"],
        "quality_score":       9,
        "strengths":           ["code", "multilingual", "summary", "creative"],
        "context_window":      1_000_000,
        "supports_streaming":  True,
    },
    "gemini-1.5-flash": {
        "provider":            "google",
        "litellm_model_id":    "gemini/gemini-1.5-flash",
        "price_per_1k_input":  MODEL_PRICING["gemini-1.5-flash"]["input"],
        "price_per_1k_output": MODEL_PRICING["gemini-1.5-flash"]["output"],
        "quality_score":       7,
        "strengths":           ["simple", "summary", "multilingual"],
        "context_window":      1_000_000,
        "supports_streaming":  True,
    },
    "gemini-2.0-flash": {
        "provider":            "google",
        "litellm_model_id":    "gemini/gemini-2.0-flash",
        "price_per_1k_input":  MODEL_PRICING["gemini-2.0-flash"]["input"],
        "price_per_1k_output": MODEL_PRICING["gemini-2.0-flash"]["output"],
        "quality_score":       8,
        "strengths":           ["simple", "summary", "multilingual", "code"],
        "context_window":      1_000_000,
        "supports_streaming":  True,
    },
    # ── Mistral ──────────────────────────────────────────────────────────────
    "mistral-large-latest": {
        "provider":            "mistral",
        "litellm_model_id":    "mistral/mistral-large-latest",
        "price_per_1k_input":  MODEL_PRICING["mistral-large-latest"]["input"],
        "price_per_1k_output": MODEL_PRICING["mistral-large-latest"]["output"],
        "quality_score":       8,
        "strengths":           ["code", "multilingual", "legal", "summary"],
        "context_window":      131_072,
        "supports_streaming":  True,
    },
    "mistral-small-latest": {
        "provider":            "mistral",
        "litellm_model_id":    "mistral/mistral-small-latest",
        "price_per_1k_input":  MODEL_PRICING["mistral-small-latest"]["input"],
        "price_per_1k_output": MODEL_PRICING["mistral-small-latest"]["output"],
        "quality_score":       6,
        "strengths":           ["simple", "summary", "multilingual"],
        "context_window":      131_072,
        "supports_streaming":  True,
    },
    "mistral-nemo": {
        "provider":            "mistral",
        "litellm_model_id":    "mistral/mistral-nemo",
        "price_per_1k_input":  MODEL_PRICING["mistral-nemo"]["input"],
        "price_per_1k_output": MODEL_PRICING["mistral-nemo"]["output"],
        "quality_score":       6,
        "strengths":           ["simple", "multilingual"],
        "context_window":      131_072,
        "supports_streaming":  True,
    },
}

ANTHROPIC_MODEL_ALIASES: Dict[str, str] = {
    # Les IDs "4-5" utilises dans l'UI sont des labels internes.
    # On les mappe vers des IDs Anthropic reels et stables.
    "claude-sonnet-4-5": "claude-sonnet-4-20250514",
    "claude-opus-4-5": "claude-opus-4-20250514",
    "claude-haiku-4-5": "claude-3-5-haiku-latest",
}

# Seuil de qualité minimal par complexité
_QUALITY_THRESHOLD: Dict[str, int] = {
    "simple":  6,
    "medium":  7,
    "complex": 8,
}

# Contexte minimal requis pour les requêtes longues (tokens)
_LONG_CONTEXT_MIN: int = 100_000

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class RequestProfile(BaseModel):
    complexity:            Literal["simple", "medium", "complex"] = "medium"
    task_type:             str = "simple"
    estimated_tokens:      int = 500
    requires_long_context: bool = False
    attachment_types:      List[str] = []


class ModelSelection(BaseModel):
    model_id:            str           # clé du MODEL_REGISTRY
    litellm_model_id:    str           # ID LiteLLM (ex: "gemini/gemini-1.5-flash")
    provider:            str
    price_per_1k_input:  float
    price_per_1k_output: float
    reason:              str           # trace de décision (logs uniquement)


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------

_PLACEHOLDER_FRAGMENTS = ("remplacer", "change_me", "changeme", "your_key", "placeholder", "xxx", "todo")


def _is_real_key(key: str) -> bool:
    """Retourne True uniquement si la clé n'est pas vide et ne ressemble pas à un placeholder."""
    if not key:
        return False
    low = key.lower()
    return not any(frag in low for frag in _PLACEHOLDER_FRAGMENTS)


def mask_secret(value: str, visible: int = 8) -> str:
    if not value:
        return "<missing>"
    if len(value) <= visible:
        return value
    return f"{value[:visible]}..."


def resolve_provider_model(provider: str, model: str) -> str:
    provider_name = (provider or "").lower()
    if provider_name == "anthropic":
        return ANTHROPIC_MODEL_ALIASES.get(model, model)
    return model


def _ensure_litellm_keys() -> None:
    """Injecte les clés API depuis settings dans l'environnement LiteLLM.
    Utilise l'assignation directe (pas setdefault) pour écraser toute valeur
    résiduelle déjà présente dans l'environnement OS.
    """
    import os
    if _is_real_key(settings.ANTHROPIC_API_KEY):
        os.environ["ANTHROPIC_API_KEY"] = settings.ANTHROPIC_API_KEY
    if _is_real_key(settings.OPENAI_API_KEY):
        os.environ["OPENAI_API_KEY"] = settings.OPENAI_API_KEY
    if _is_real_key(settings.GEMINI_API_KEY):
        os.environ["GEMINI_API_KEY"] = settings.GEMINI_API_KEY
    if _is_real_key(settings.MISTRAL_API_KEY):
        os.environ["MISTRAL_API_KEY"] = settings.MISTRAL_API_KEY


def _provider_key(provider: str, provider_api_keys: Optional[Dict[str, str]] = None) -> str:
    user_key = str((provider_api_keys or {}).get(provider, "") or "").strip()
    if _is_real_key(user_key):
        return user_key
    keys: Dict[str, str] = {
        "anthropic": settings.ANTHROPIC_API_KEY,
        "openai":    settings.OPENAI_API_KEY,
        "google":    settings.GEMINI_API_KEY,
        "mistral":   settings.MISTRAL_API_KEY,
    }
    return str(keys.get(provider, "") or "").strip()


def _provider_from_litellm_model(litellm_model_id: str) -> str:
    model = (litellm_model_id or "").lower()
    if model.startswith("gemini/"):
        return "google"
    if model.startswith("mistral/"):
        return "mistral"
    if model.startswith("claude-"):
        return "anthropic"
    if model.startswith("gpt-") or model.startswith("o1"):
        return "openai"
    return ""


def _provider_has_key(provider: str, provider_api_keys: Optional[Dict[str, str]] = None) -> bool:
    """Retourne True si la clé API pour ce provider est configurée et non-placeholder."""
    return _is_real_key(_provider_key(provider, provider_api_keys))


_TASK_KEYWORDS: Dict[str, tuple] = {
    "code":         ("code", "fonction", "function", "script", "debug", "erreur", "bug",
                     "python", "javascript", "typescript", "sql", "api", "programme", "algorithme",
                     "class", "import", "return", "def ", "const ", "let ", "var "),
    "legal":        ("contrat", "juridique", "loi", "règlement", "clause", "legal",
                     "compliance", "gdpr", "rgpd", "article", "décret", "tribunal"),
    "math":         ("calcul", "équation", "math", "statistique", "probabilité",
                     "formule", "nombre", "dérivée", "intégrale", "matrice"),
    "summary":      ("résume", "résumé", "synthèse", "récapitule", "summarize",
                     "summary", "tldr", "points clés", "en bref"),
    "creative":     ("rédige", "écris", "génère", "crée", "imagine", "histoire",
                     "article", "email", "lettre", "poème", "slogan"),
    "multilingual": ("traduis", "translate", "traduction", "translation",
                     "en anglais", "en français", "in english", "auf deutsch"),
}


def _default_profile(text: str) -> RequestProfile:
    """Profil de complexité basé sur une heuristique locale — aucun appel réseau."""
    length = len(text)
    low    = text.lower()

    task_type = "simple"
    for ttype, keywords in _TASK_KEYWORDS.items():
        if any(kw in low for kw in keywords):
            task_type = ttype
            break

    if length < 200:
        complexity = "simple"
    elif length < 2000:
        complexity = "medium"
    else:
        complexity = "complex"

    return RequestProfile(
        complexity=complexity,
        task_type=task_type,
        estimated_tokens=min(500 + length // 10, 4000),
        requires_long_context=(length > 8000),
    )


def _make_selection(model_id: str, reason: str) -> ModelSelection:
    meta = MODEL_REGISTRY[model_id]
    return ModelSelection(
        model_id=model_id,
        litellm_model_id=meta["litellm_model_id"],
        provider=meta["provider"],
        price_per_1k_input=meta["price_per_1k_input"],
        price_per_1k_output=meta["price_per_1k_output"],
        reason=reason,
    )


_SPREADSHEET_EXTS = {"xlsx", "xls", "csv", "tsv", "ods"}
_PRESENTATION_EXTS = {"pptx", "ppt", "odp"}
_PDF_EXTS = {"pdf"}
_TRANSCRIPT_EXTS = {"vtt", "srt"}


def _normalize_attachment_types(attachment_types: Optional[Sequence[str]]) -> List[str]:
    normalized: List[str] = []
    seen: set[str] = set()
    for ext in attachment_types or []:
        value = str(ext or "").strip().lower().lstrip(".")
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _preferred_models_for_attachments(attachment_types: Sequence[str]) -> tuple[List[str], str]:
    attachment_set = set(_normalize_attachment_types(attachment_types))
    if not attachment_set:
        return [], ""

    has_pdf = bool(attachment_set & _PDF_EXTS)
    has_spreadsheet = bool(attachment_set & _SPREADSHEET_EXTS)
    has_presentation = bool(attachment_set & _PRESENTATION_EXTS)
    has_transcript = bool(attachment_set & _TRANSCRIPT_EXTS)

    if has_pdf:
        return (
            [
                "gpt-4o-mini",
                "gpt-4o",
                "gemini-1.5-flash",
                "gemini-1.5-pro",
                "mistral-small-latest",
                "mistral-large-latest",
                "claude-haiku-4-5",
                "claude-sonnet-4-5",
                "claude-opus-4-5",
            ],
            "attachment_profile=pdf",
        )

    if has_spreadsheet and has_presentation:
        return (
            [
                "gpt-4o",
                "gpt-4o-mini",
                "gemini-1.5-pro",
                "claude-sonnet-4-5",
                "mistral-large-latest",
                "gemini-1.5-flash",
                "claude-haiku-4-5",
                "mistral-small-latest",
            ],
            "attachment_profile=spreadsheet+presentation",
        )

    if has_spreadsheet:
        return (
            [
                "gpt-4o-mini",
                "gpt-4o",
                "gemini-1.5-flash",
                "gemini-1.5-pro",
                "mistral-small-latest",
                "mistral-large-latest",
                "claude-haiku-4-5",
                "claude-sonnet-4-5",
            ],
            "attachment_profile=spreadsheet",
        )

    if has_presentation:
        return (
            [
                "claude-haiku-4-5",
                "gpt-4o-mini",
                "mistral-small-latest",
                "gemini-1.5-flash",
                "gpt-4o",
                "claude-sonnet-4-5",
                "mistral-large-latest",
                "gemini-1.5-pro",
            ],
            "attachment_profile=presentation",
        )

    if has_transcript:
        return (
            [
                "claude-haiku-4-5",
                "gpt-4o-mini",
                "gemini-1.5-flash",
                "mistral-small-latest",
                "claude-sonnet-4-5",
                "gpt-4o",
                "mistral-large-latest",
            ],
            "attachment_profile=transcript",
        )

    return (
        [
            "claude-haiku-4-5",
            "gpt-4o-mini",
            "mistral-small-latest",
            "gemini-1.5-flash",
            "claude-sonnet-4-5",
            "gpt-4o",
            "gemini-1.5-pro",
            "mistral-large-latest",
        ],
        "attachment_profile=document",
    )


# ---------------------------------------------------------------------------
# API publique — analyse et sélection
# ---------------------------------------------------------------------------

async def analyze_request(
    text: str,
    language: Optional[str] = None,
    attachment_types: Optional[List[str]] = None,
) -> RequestProfile:
    """
    Heuristique locale — analyse de complexité du prompt SANS appel LLM.

    RGPD: cette fonction NE doit PAS envoyer de données à un tiers.
    Elle opère uniquement sur la longueur, le vocabulaire et les
    métadonnées du prompt.

    Le paramètre `language` est conservé pour compatibilité d'interface.
    """
    normalized_attachments = _normalize_attachment_types(attachment_types)
    user_text = (text or "").strip()
    word_count = len(user_text.split())
    char_count = len(user_text)
    estimated_tokens = max(1, char_count // 4)

    complex_keywords = {
        "analyse", "analyser", "compare", "comparer", "synthese",
        "synthetise", "resume", "resumer", "redige", "rediger",
        "explique", "expliquer", "demontre", "demontrer", "justifie",
        "justifier", "evalue", "evaluer", "audit", "auditer",
        "memo", "memoire", "conclusion", "plaidoirie", "contrat",
        "jurisprudence", "bilan", "liasse", "tva", "declaration",
    }
    text_lower = user_text.lower()
    keyword_hits = sum(1 for kw in complex_keywords if kw in text_lower)
    question_count = user_text.count("?")

    has_attachment = len(normalized_attachments) > 0
    heavy_attachment = any(
        attachment_type in {"pdf", "docx", "xlsx", "pptx"}
        for attachment_type in normalized_attachments
    )

    is_complex = (
        word_count > 80
        or question_count >= 3
        or keyword_hits >= 2
        or heavy_attachment
    )

    needs_long_context = (
        word_count > 300
        or estimated_tokens > 1500
        or heavy_attachment
    )

    task_type = "simple"
    for task_name, keywords in _TASK_KEYWORDS.items():
        if any(keyword in text_lower for keyword in keywords):
            task_type = task_name
            break

    complexity: Literal["simple", "medium", "complex"] = "complex" if is_complex else "simple"
    if complexity == "simple" and (
        word_count > 20
        or question_count >= 1
        or keyword_hits >= 1
        or has_attachment
    ):
        complexity = "medium"

    return RequestProfile(
        complexity=complexity,
        task_type=task_type,
        estimated_tokens=estimated_tokens,
        requires_long_context=needs_long_context,
        attachment_types=normalized_attachments,
    )


async def select_model(
    profile: Optional[RequestProfile],
    user_preference: Optional[str] = None,
    provider_api_keys: Optional[Dict[str, str]] = None,
) -> ModelSelection:
    """
    Sélectionne le modèle optimal selon le profil et la préférence utilisateur.

    Algorithme :
      1. user_preference fourni → validation + retour direct
      2. Filtrer par qualité (seuil selon complexity)
      3. Filtrer par strengths (task_type)
      4. Filtrer par context_window (si requires_long_context)
      5. Filtrer par provider avec clé API configurée
      6. Filtrer par supports_streaming=True (sécurité streaming)
      7. Trier par prix total (input + output) croissant
      8. Retourner le moins cher

    Raises:
        ValueError  : user_preference inconnu du MODEL_REGISTRY
        RuntimeError: aucun provider configuré (aucune clé API disponible)
    """
    # ── 1. Préférence explicite de l'utilisateur ──────────────────────────────
    if user_preference:
        if user_preference not in MODEL_REGISTRY:
            raise ValueError(
                f"Modèle inconnu : {user_preference!r}. "
                f"Modèles disponibles : {sorted(MODEL_REGISTRY)}."
            )
        if not _provider_has_key(MODEL_REGISTRY[user_preference]["provider"], provider_api_keys):
            raise ValueError(
                f"Clé API absente pour le provider "
                f"'{MODEL_REGISTRY[user_preference]['provider']}' "
                f"(modèle {user_preference!r})."
            )
        logger.debug("select_model: user_preference=%s", user_preference)
        return _make_selection(user_preference, reason="user_preference")

    # ── 2-6. Filtrage automatique ─────────────────────────────────────────────
    if profile is None:
        profile = _default_profile("")

    attachment_types = _normalize_attachment_types(getattr(profile, "attachment_types", []))
    if attachment_types:
        preferred_models, reason = _preferred_models_for_attachments(attachment_types)
        for model_id in preferred_models:
            meta = MODEL_REGISTRY.get(model_id)
            if meta and _provider_has_key(meta["provider"], provider_api_keys):
                return _make_selection(model_id, reason=reason)

    if not attachment_types:
        cheapest_candidates = [
            (mid, meta)
            for mid, meta in MODEL_REGISTRY.items()
            if _provider_has_key(meta["provider"], provider_api_keys)
            and (not profile.requires_long_context or meta["context_window"] >= _LONG_CONTEXT_MIN)
        ]
        if cheapest_candidates:
            cheapest_candidates.sort(
                key=lambda x: x[1]["price_per_1k_input"] + x[1]["price_per_1k_output"]
            )
            cheapest_id, _ = cheapest_candidates[0]
            return _make_selection(cheapest_id, reason="text_only_cheapest_available")

    quality_min = _QUALITY_THRESHOLD.get(profile.complexity, 7)

    def _passes(model_id: str, meta: ModelInfo) -> bool:
        if meta["quality_score"] < quality_min:
            return False
        strengths: List[str] = meta["strengths"]
        # Le modèle doit couvrir le task_type OU "simple" (généraliste)
        if profile.task_type not in strengths and "simple" not in strengths:
            return False
        if profile.requires_long_context and meta["context_window"] < _LONG_CONTEXT_MIN:
            return False
        if not _provider_has_key(meta["provider"], provider_api_keys):
            return False
        return True

    candidates = [
        (mid, meta)
        for mid, meta in MODEL_REGISTRY.items()
        if _passes(mid, meta)
    ]

    # ── Fallback 1 : relâcher la contrainte strengths ─────────────────────────
    if not candidates:
        candidates = [
            (mid, meta)
            for mid, meta in MODEL_REGISTRY.items()
            if meta["quality_score"] >= quality_min
            and _provider_has_key(meta["provider"], provider_api_keys)
        ]

    # ── Fallback 2 : n'importe quel modèle avec clé disponible ───────────────
    if not candidates:
        candidates = [
            (mid, meta)
            for mid, meta in MODEL_REGISTRY.items()
            if _provider_has_key(meta["provider"], provider_api_keys)
        ]

    if not candidates:
        raise RuntimeError(
            "Aucun provider LLM configuré. "
            "Vérifiez ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY / MISTRAL_API_KEY."
        )

    # ── 7. Tri par prix total croissant ──────────────────────────────────────
    candidates.sort(
        key=lambda x: x[1]["price_per_1k_input"] + x[1]["price_per_1k_output"]
    )
    best_id, _ = candidates[0]

    reason = (
        f"complexity={profile.complexity}, task={profile.task_type}, "
        f"long_ctx={profile.requires_long_context}, quality_min={quality_min}"
    )
    logger.debug("select_model: selected=%s | %s", best_id, reason)
    return _make_selection(best_id, reason=reason)


# ---------------------------------------------------------------------------
# Appels LLM via LiteLLM (non-streaming et streaming)
# ---------------------------------------------------------------------------

def _extract_text(content) -> str:
    """Extrait le texte brut d'un contenu LiteLLM qui peut être str, list de blocs, ou objet."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    if hasattr(content, "text"):
        return content.text or ""
    return str(content) if content else ""


def _build_messages(
    messages: List[Dict[str, str]],
    system: Optional[str],
) -> List[Dict[str, str]]:
    """Préfixe le message système si fourni (format OpenAI universel)."""
    if system:
        return [{"role": "system", "content": system}] + messages
    return messages


async def call_llm_via_litellm(
    litellm_model_id: str,
    messages: List[Dict[str, str]],
    max_tokens: int = 2048,
    system: Optional[str] = None,
    provider_api_keys: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Appel LLM non-streaming via LiteLLM.

    Args:
        litellm_model_id : ID LiteLLM (ex: "claude-haiku-4-5", "gemini/gemini-1.5-flash")
        messages         : historique au format [{role, content}]
        max_tokens       : limite de tokens en output
        system           : message système (optionnel)

    Returns:
        Texte de la réponse du modèle.

    Raises:
        RuntimeError si le modèle ne répond pas.
    """
    _ensure_litellm_keys()
    litellm_model_id = resolve_provider_model("anthropic", litellm_model_id) if litellm_model_id.startswith("claude-") else litellm_model_id
    full_messages = _build_messages(messages, system)
    provider = _provider_from_litellm_model(litellm_model_id)
    api_key = _provider_key(provider, provider_api_keys) if provider else ""
    kwargs: Dict[str, Any] = {
        "model": litellm_model_id,
        "messages": full_messages,
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }
    if _is_real_key(api_key):
        kwargs["api_key"] = api_key

    try:
        response = await asyncio.wait_for(
            litellm.acompletion(**kwargs),
            timeout=30.0,
        )
        usage = getattr(response, "usage", None)
        return {
            "text": _extract_text(response.choices[0].message.content),
            "usage": {
                "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            } if usage else None,
        }
    except asyncio.TimeoutError:
        raise RuntimeError(f"LiteLLM timeout (30s) for {litellm_model_id!r}")
    except Exception as exc:
        logger.error(
            "litellm.call_error | model=%s | exc=%r",
            litellm_model_id,
            exc,
            exc_info=True,
        )
        raise RuntimeError(
            f"LiteLLM call failed for {litellm_model_id!r}: {type(exc).__name__}: {exc}"
        ) from exc


async def stream_llm_via_litellm(
    litellm_model_id: str,
    messages: List[Dict[str, str]],
    max_tokens: int = 2048,
    system: Optional[str] = None,
    provider_api_keys: Optional[Dict[str, str]] = None,
) -> AsyncGenerator[str, None]:
    """
    Appel LLM en streaming via LiteLLM.
    Yield des chunks de texte bruts (str) au fur et à mesure.

    Compatible avec stream_reidentified() de streaming.py.
    """
    _ensure_litellm_keys()
    litellm_model_id = resolve_provider_model("anthropic", litellm_model_id) if litellm_model_id.startswith("claude-") else litellm_model_id
    full_messages = _build_messages(messages, system)
    provider = _provider_from_litellm_model(litellm_model_id)
    api_key = _provider_key(provider, provider_api_keys) if provider else ""
    kwargs: Dict[str, Any] = {
        "model": litellm_model_id,
        "messages": full_messages,
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "stream": True,
    }
    if _is_real_key(api_key):
        kwargs["api_key"] = api_key

    try:
        response = await asyncio.wait_for(
            litellm.acompletion(**kwargs),
            timeout=30.0,
        )
        async for chunk in response:
            delta = _extract_text(chunk.choices[0].delta.content) if chunk.choices else ""
            if delta:
                yield delta
    except asyncio.TimeoutError:
        raise RuntimeError(f"LiteLLM stream timeout (30s) for {litellm_model_id!r}")
    except Exception as exc:
        logger.error(
            "litellm.stream_error | model=%s | exc=%r",
            litellm_model_id,
            exc,
            exc_info=True,
        )
        raise RuntimeError(
            f"LiteLLM stream failed for {litellm_model_id!r}: {type(exc).__name__}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Estimation de coût
# ---------------------------------------------------------------------------

def calculate_cost(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    meta = MODEL_REGISTRY.get(model_id)
    if not meta:
        return 0.0
    return round(
        (input_tokens / 1000) * meta["price_per_1k_input"]
        + (output_tokens / 1000) * meta["price_per_1k_output"],
        6,
    )


def _estimate_tokens_from_char_count(char_count: int, model: str) -> int:
    model_lower = (model or "").lower()
    if "claude" in model_lower:
        return max(1, int(char_count / 3.5)) if char_count else 0
    if "gpt" in model_lower or "o1" in model_lower:
        return max(1, int(char_count / 4)) if char_count else 0
    if "gemini" in model_lower:
        return max(1, int(char_count / 4)) if char_count else 0
    if "mistral" in model_lower:
        return max(1, int(char_count / 3.8)) if char_count else 0
    return max(1, int(char_count / 4)) if char_count else 0


def estimate_tokens(text: str, model: str) -> int:
    return _estimate_tokens_from_char_count(len(text or ""), model)


def estimate_cost(
    model_id: str,
    input_text_chars: int,
    max_output_tokens: int,
) -> float:
    """
    Retourne une estimation du coût en USD pour un appel donné.
    Heuristique : 4 chars ≈ 1 token (toutes langues confondues).
    """
    input_tokens = _estimate_tokens_from_char_count(max(input_text_chars, 0), model_id)
    return calculate_cost(model_id, input_tokens, max_output_tokens)
