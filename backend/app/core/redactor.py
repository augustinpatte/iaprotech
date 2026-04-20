"""
Redactor — Production-ready multilingual PII pseudonymisation engine.

Architecture :
  • langdetect détecte la langue du texte entrant
  • Le bon modèle spaCy est chargé (et mis en cache) selon la langue
  • Microsoft Presidio analyse les entités PII avec des recognizers custom
  • Les entités sont remplacées par des tokens numérotés : [PERSONNE_1], [EMAIL_1]…
  • Le mapping inverse permet la réidentification après réponse du LLM

Entités détectées : PERSON, EMAIL_ADDRESS, PHONE_NUMBER, IBAN_CODE,
  SIRET, VAT_NUMBER, LOCATION, DATE_TIME, CREDIT_CARD, IP_ADDRESS, URL, ORGANIZATION

Performance cible : < 150 ms pour 500 mots.
"""

import asyncio
import importlib.util
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from langdetect import detect, LangDetectException
from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer, RecognizerRegistry
from presidio_analyzer.nlp_engine import NlpEngineProvider

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)
CPU_COUNT = os.cpu_count() or 2

# ---------------------------------------------------------------------------
# Configuration : langue → modèle spaCy
# ---------------------------------------------------------------------------

LANG_TO_SPACY_MODEL: Dict[str, str] = {
    "fr": "fr_core_news_lg",
    "en": "en_core_web_lg",
    "de": "de_core_news_lg",
    "es": "es_core_news_lg",
    "it": "it_core_news_lg",
    "pt": "pt_core_news_lg",
    "nl": "nl_core_news_lg",
}
_FALLBACK_LANG = "en"

# Labels humains pour les tokens (en français)
ENTITY_TO_LABEL: Dict[str, str] = {
    "PERSON":        "PERSONNE",
    "EMAIL_ADDRESS": "EMAIL",
    "PHONE_NUMBER":  "TELEPHONE",
    "IBAN_CODE":     "IBAN",
    "SIRET":         "SIRET",
    "VAT_NUMBER":    "TVA",
    "LOCATION":      "ADRESSE",
    "DATE_TIME":     "DATE",
    "CREDIT_CARD":   "CARTE",
    "IP_ADDRESS":    "IP",
    "URL":           "URL",
    "ORGANIZATION":  "ORGANISATION",
    "NRP":           "ID_NATIONAL",
    "FR_NIR":        "NIR",
    "SALARY":        "SALAIRE",
    "CONTRACT":      "CONTRAT",
}

# Entités activables uniquement via des PatternRecognizers (pas de NLP spaCy requis)
_PATTERN_ONLY_ENTITIES: frozenset[str] = frozenset({
    "EMAIL_ADDRESS", "PHONE_NUMBER", "IBAN_CODE", "CREDIT_CARD",
    "IP_ADDRESS", "URL", "SIRET", "VAT_NUMBER", "MEDICAL_LICENSE",
    "FR_NIR", "SALARY", "CONTRACT",
})

_BUSINESS_REASON_ENTITIES: frozenset[str] = frozenset({
    "SALARY",
    "CONTRACT",
})


@dataclass
class PseudonymizationResult:
    pseudonymized_text: str
    mapping: Dict[str, str]
    explanation: List[dict]


# ---------------------------------------------------------------------------
# Recognizers custom
# ---------------------------------------------------------------------------

def _build_siret_recognizer() -> PatternRecognizer:
    """
    SIRET : 14 chiffres, parfois formaté en blocs (123 456 789 01234).
    Supporté uniquement en français.
    """
    return PatternRecognizer(
        supported_entity="SIRET",
        supported_language="fr",
        patterns=[
            Pattern(
                name="siret_14_digits",
                # 3+3+3+5 = 14 chiffres, espaces optionnels entre blocs
                regex=r"(?<!\d)\d{3}[\s]?\d{3}[\s]?\d{3}[\s]?\d{5}(?!\d)",
                score=0.85,
            ),
        ],
        context=["siret", "établissement", "siren", "numéro d'entreprise"],
    )


def _build_vat_recognizer() -> PatternRecognizer:
    """
    Numéros de TVA intracommunautaire (UE).
    Couvre : FR, DE, ES, IT, GB, BE, NL, PT.
    """
    vat_regex = (
        r"\b("
        r"FR[A-Z0-9]{2}\d{9}"               # France : FR + 2 alphanum + 9 chiffres
        r"|DE\d{9}"                           # Allemagne
        r"|ES[A-Z0-9]\d{7}[A-Z0-9]"          # Espagne
        r"|IT\d{11}"                          # Italie
        r"|GB(?:\d{9}|\d{12}|GD\d{3}|HA\d{3})"  # Royaume-Uni
        r"|BE0\d{9}"                          # Belgique
        r"|NL\d{9}B\d{2}"                    # Pays-Bas
        r"|PT\d{9}"                           # Portugal
        r")\b"
    )
    return PatternRecognizer(
        supported_entity="VAT_NUMBER",
        patterns=[Pattern(name="eu_vat", regex=vat_regex, score=0.9)],
        context=["tva", "vat", "ust-id", "numéro fiscal", "intracommunautaire"],
    )


def _build_extended_iban_recognizer() -> PatternRecognizer:
    """
    IBAN étendu avec regex par pays (FR, DE, ES, IT, GB, NL, BE, PT).
    Complète le recognizer IBAN natif de Presidio.
    """
    iban_regex = (
        r"\b("
        # France  : FR + 2 + 23 chars = 27
        r"FR\d{2}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{3}"
        # Allemagne : DE + 2 + 18 = 22
        r"|DE\d{2}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{2}"
        # Espagne : ES + 2 + 20 = 24
        r"|ES\d{2}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}"
        # Italie : IT + 2 + 1 lettre + 22 = 27
        r"|IT\d{2}[A-Z][\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{3}"
        # Royaume-Uni : GB + 2 + 4 lettres + 14 = 22
        r"|GB\d{2}[A-Z]{4}[\s]?\d{6}[\s]?\d{8}"
        # Pays-Bas : NL + 2 + 4 lettres + 10 = 18
        r"|NL\d{2}[A-Z]{4}[\s]?\d{10}"
        # Belgique : BE + 2 + 12 = 16
        r"|BE\d{2}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}"
        # Portugal : PT + 2 + 21 = 25
        r"|PT\d{2}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d"
        r")\b"
    )
    return PatternRecognizer(
        supported_entity="IBAN_CODE",
        patterns=[Pattern(name="iban_extended", regex=iban_regex, score=0.82)],
        context=["iban", "rib", "compte", "virement", "bank", "bic", "konto"],
    )



def _build_nir_recognizer() -> PatternRecognizer:
    """NIR (numero de securite sociale francais) — 13 chiffres + cle 2 chiffres."""
    return PatternRecognizer(
        supported_entity="FR_NIR",
        supported_language="fr",
        patterns=[
            Pattern(
                name="fr_nir_raw",
                regex=r"(?<!\d)[12][0-9]{2}(?:0[1-9]|1[0-2]|20)[0-9]{5}[0-9]{3}[0-9]{2}(?!\d)",
                score=0.90,
            ),
            Pattern(
                name="fr_nir_spaced",
                regex=r"(?<!\d)[12]\s[0-9]{2}\s(?:0[1-9]|1[0-2]|20)\s[0-9]{2}\s[0-9]{3}\s[0-9]{3}\s[0-9]{2}(?!\d)",
                score=0.90,
            ),
        ],
        context=["nir", "secu", "securite sociale", "num secu", "immatriculation", "ss"],
    )


def _build_salary_recognizer() -> PatternRecognizer:
    """Montants de salaires et remuneration."""
    salary_regex = (
        r"(?i)(?:"
        r"salaire\s*[:=]\s*[\d\s,\.]+\s*[\u20ac$]"
        r"|r[\u00e9e]mun[\u00e9e]ration\s+de\s+[\d\s,\.]+\s*[\u20ac$]"
        r"|[\d]{1,3}(?:[\s][0-9]{3})*\s*[\u20ac]\s*(?:brut|net)"
        r"|(?:brut|net)\s*(?:mensuel|annuel)?\s*[:=]?\s*[\d\s,\.]+\s*[\u20ac$]"
        r")"
    )
    return PatternRecognizer(
        supported_entity="SALARY",
        patterns=[Pattern(name="salary_amount", regex=salary_regex, score=0.75)],
        context=["salaire", "remuneration", "paie", "traitement", "indemnite", "revenu", "brut", "net"],
    )


def _build_contract_recognizer() -> PatternRecognizer:
    """Numeros de contrats internes (CTR-XXXX, CONT-XXXX, etc.)."""
    contract_regex = (
        r"(?i)\b(?:"
        r"CTR[-_][0-9]{2,4}[-_][A-Z0-9]{2,8}"
        r"|CONT[-_][0-9]{4,8}"
        r"|CONTRAT[-_ ]?N[\u00b0o]?\s*[A-Z0-9]{4,12}"
        r"|N[\u00b0o]?\s*(?:DE\s+)?CONTRAT\s*:?\s*[A-Z0-9]{4,12}"
        r")\b"
    )
    return PatternRecognizer(
        supported_entity="CONTRACT",
        patterns=[Pattern(name="contract_internal", regex=contract_regex, score=0.80)],
        context=["contrat", "contract", "ctr", "reference", "numero", "n°"],
    )


# ---------------------------------------------------------------------------
# Classe principale
# ---------------------------------------------------------------------------

class Redactor:
    """
    Moteur de pseudonymisation PII multilingue basé sur Presidio + spaCy.

    Thread-safe. L'AnalyzerEngine et les modèles spaCy sont initialisés une
    seule fois et partagés entre les requêtes concurrentes.

    Usage typique :
        redactor = Redactor()
        pseudonymised, mapping = await redactor.smart_pseudonymize(text)
        restored = redactor.reidentify(llm_response, mapping)
    """

    def __init__(self) -> None:
        # Thread pool : Presidio est synchrone, on l'isole pour ne pas bloquer la boucle asyncio
        self._executor = ThreadPoolExecutor(
            max_workers=min(CPU_COUNT, 4),
            thread_name_prefix="redactor",
        )
        self._local_mode: bool = (settings.REDACTION_ENGINE == "local")
        self._analyzers: Dict[str, AnalyzerEngine] = {}
        self._analyzer_lock = threading.Lock()
        self._supported_langs = self._discover_supported_languages()

        # Entités actives : intersection de REDACTION_ENTITIES (settings) et des types connus
        _known = set(ENTITY_TO_LABEL.keys())
        _requested = set(settings.REDACTION_ENTITIES)
        active = _requested & _known
        if self._local_mode:
            # Mode local : uniquement les entités détectables par regex, sans NLP spaCy
            active = active & _PATTERN_ONLY_ENTITIES
        # Fallback : si le filtrage vide la liste, on active toutes les entités connues
        self._active_entities: list[str] = sorted(active) if active else sorted(_known)

        logger.info(
            "Redactor initialisé — engine=%s | langues=%s | entités=%d",
            settings.REDACTION_ENGINE, self._supported_langs, len(self._active_entities),
        )

    # ------------------------------------------------------------------
    # Construction de l'AnalyzerEngine
    # ------------------------------------------------------------------

    def _discover_supported_languages(self) -> List[str]:
        """Détecte les modèles spaCy installés sans les charger en mémoire."""
        supported_langs: List[str] = []
        for lang in settings.SUPPORTED_LANGUAGES:
            model_name = LANG_TO_SPACY_MODEL.get(lang)
            if not model_name:
                logger.warning("Aucun modèle spaCy configuré pour lang='%s' — ignoré.", lang)
                continue
            if importlib.util.find_spec(model_name) is None:
                logger.warning(
                    "Modèle spaCy '%s' absent — lang='%s' ignoré. Installer : python -m spacy download %s",
                    model_name, lang, model_name,
                )
                continue
            supported_langs.append(lang)

        if not supported_langs:
            logger.warning(
                "Aucun modèle spaCy installé détecté — fallback langue='%s'",
                _FALLBACK_LANG,
            )
            supported_langs.append(_FALLBACK_LANG)
        elif _FALLBACK_LANG not in supported_langs:
            supported_langs.append(_FALLBACK_LANG)
            logger.warning("Fallback anglais activé pour Presidio.")

        return supported_langs

    def _build_analyzer(self, lang: str) -> AnalyzerEngine:
        """
        Construit un AnalyzerEngine Presidio pour une seule langue.
        Les gros modèles spaCy sont chargés à la demande pour éviter
        un pic mémoire au premier message.
        """
        effective_lang = lang if lang in self._supported_langs else _FALLBACK_LANG
        model_name = LANG_TO_SPACY_MODEL.get(effective_lang) or LANG_TO_SPACY_MODEL[_FALLBACK_LANG]

        if importlib.util.find_spec(model_name) is None:
            raise RuntimeError(
                f"Modèle spaCy indisponible pour lang='{effective_lang}' ({model_name})."
            )

        nlp_config = {
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": effective_lang, "model_name": model_name}],
        }
        provider = NlpEngineProvider(nlp_configuration=nlp_config)
        nlp_engine = provider.create_engine()
        supported_langs = [effective_lang]

        registry = RecognizerRegistry(supported_languages=supported_langs)
        registry.load_predefined_recognizers(languages=supported_langs, nlp_engine=nlp_engine)

        # Ajout des recognizers custom
        for recognizer in [
            _build_siret_recognizer(),
            _build_vat_recognizer(),
            _build_extended_iban_recognizer(),
            _build_nir_recognizer(),
            _build_salary_recognizer(),
            _build_contract_recognizer(),
        ]:
            registry.add_recognizer(recognizer)

        analyzer = AnalyzerEngine(
            nlp_engine=nlp_engine,
            registry=registry,
            supported_languages=supported_langs,
        )
        logger.info(
            "Analyzer charge a la demande | lang=%s | model=%s",
            effective_lang,
            model_name,
        )
        return analyzer

    def _get_analyzer(self, lang: str) -> AnalyzerEngine:
        effective_lang = lang if lang in self._supported_langs else _FALLBACK_LANG
        analyzer = self._analyzers.get(effective_lang)
        if analyzer is not None:
            return analyzer

        with self._analyzer_lock:
            analyzer = self._analyzers.get(effective_lang)
            if analyzer is None:
                analyzer = self._build_analyzer(effective_lang)
                self._analyzers[effective_lang] = analyzer
        return analyzer

    # ------------------------------------------------------------------
    # Détection de langue
    # ------------------------------------------------------------------

    def _detect_language(self, text: str) -> str:
        """
        Détecte la langue du texte avec langdetect.
        Retourne un code ISO 639-1 supporté, 'en' en fallback.
        """
        try:
            lang = detect(text[:1500])  # échantillon pour la vitesse
            if lang in self._supported_langs:
                return lang
            # Variantes régionales (ex: "zh-cn" → "zh")
            base = lang.split("-")[0]
            if base in self._supported_langs:
                return base
            logger.debug(
                "Langue détectée '%s' non supportée → fallback '%s'",
                lang, _FALLBACK_LANG,
            )
        except LangDetectException:
            logger.debug("Détection de langue échouée → fallback '%s'", _FALLBACK_LANG)
        return _FALLBACK_LANG

    # ------------------------------------------------------------------
    # Logique de pseudonymisation (synchrone)
    # ------------------------------------------------------------------

    def _pseudonymize_sync(
        self,
        text: str,
        language: Optional[str] = None,
        entities: Optional[List[str]] = None,
        score_threshold: float = 0.6,
        excluded_entity_types: Optional[List[str]] = None,
    ) -> Tuple[str, Dict[str, str]]:
        """
        Détecte les entités PII, les remplace par des tokens numérotés.

        Déduplication : si la même valeur apparaît plusieurs fois, elle reçoit
        le même token (ex: "Jean Dupont" → toujours [PERSONNE_1]).

        Retourne :
            pseudonymised_text  — texte avec tokens
            mapping             — {token: valeur_originale}
        """
        detailed = self._pseudonymize_detailed_sync(
            text=text,
            language=language,
            entities=entities,
            score_threshold=score_threshold,
            excluded_entity_types=excluded_entity_types,
        )
        return detailed.pseudonymized_text, detailed.mapping

    def _analyze_entities(
        self,
        text: str,
        language: Optional[str] = None,
        entities: Optional[List[str]] = None,
        score_threshold: float = 0.6,
        excluded_entity_types: Optional[List[str]] = None,
    ) -> Tuple[str, list]:
        lang = language or self._detect_language(text)
        effective_lang = lang if lang in self._supported_langs else _FALLBACK_LANG
        active_entities = entities if entities is not None else list(self._active_entities)
        excluded = set(excluded_entity_types or [])
        analyzer = self._get_analyzer(effective_lang)
        results = analyzer.analyze(
            text=text,
            language=effective_lang,
            entities=active_entities,
            score_threshold=score_threshold,
        )
        filtered = _remove_overlaps(results) if results else []
        if excluded:
            filtered = [result for result in filtered if result.entity_type not in excluded]
        return effective_lang, filtered

    def _build_explanation_item(
        self,
        entity_type: str,
        original: str,
        placeholder: str,
    ) -> dict:
        reason = (
            "Donnée financière confidentielle"
            if entity_type in _BUSINESS_REASON_ENTITIES
            else "Donnée personnelle — Art. 5 RGPD"
        )
        preview = f"{original[:1]}***" if original else "***"
        return {
            "entity_type": entity_type,
            "original_preview": preview,
            "placeholder": placeholder,
            "reason": reason,
        }

    def _pseudonymize_detailed_sync(
        self,
        text: str,
        language: Optional[str] = None,
        entities: Optional[List[str]] = None,
        score_threshold: float = 0.6,
        excluded_entity_types: Optional[List[str]] = None,
    ) -> PseudonymizationResult:
        if not text or not text.strip():
            return PseudonymizationResult(
                pseudonymized_text=text,
                mapping={},
                explanation=[],
            )

        lang, results = self._analyze_entities(
            text=text,
            language=language,
            entities=entities,
            score_threshold=score_threshold,
            excluded_entity_types=excluded_entity_types,
        )
        if not results:
            return PseudonymizationResult(
                pseudonymized_text=text,
                mapping={},
                explanation=[],
            )

        value_to_token: Dict[str, str] = {}
        counters: Dict[str, int] = {}
        explanation: List[dict] = []

        for result in results:
            original = text[result.start:result.end]
            if original not in value_to_token:
                label = ENTITY_TO_LABEL.get(result.entity_type, result.entity_type)
                counters[label] = counters.get(label, 0) + 1
                token = f"[{label}_{counters[label]}]"
                value_to_token[original] = token
                explanation.append(
                    self._build_explanation_item(
                        entity_type=result.entity_type,
                        original=original,
                        placeholder=token,
                    )
                )

        chars = list(text)
        for result in sorted(results, key=lambda r: r.start, reverse=True):
            original = text[result.start:result.end]
            token = value_to_token[original]
            chars[result.start:result.end] = list(token)

        pseudonymised = "".join(chars)
        mapping: Dict[str, str] = {v: k for k, v in value_to_token.items()}

        logger.debug(
            "Pseudonymisé %d entités | %d chars | lang=%s",
            len(results), len(text), lang,
        )
        return PseudonymizationResult(
            pseudonymized_text=pseudonymised,
            mapping=mapping,
            explanation=explanation,
        )

    # ------------------------------------------------------------------
    # API publique async
    # ------------------------------------------------------------------

    async def smart_pseudonymize(
        self,
        text: str,
        language: Optional[str] = None,
        entities: Optional[List[str]] = None,
        score_threshold: float = 0.6,
        excluded_entity_types: Optional[List[str]] = None,
    ) -> Tuple[str, Dict[str, str]]:
        """
        Détecte et pseudonymise les entités PII dans *text*.

        Args:
            text     : texte brut (langue détectée automatiquement)
            language : forcer un code ISO 639-1 (skip auto-détection)

        Returns:
            (texte_pseudonymisé, mapping)
            mapping = {"[PERSONNE_1]": "Jean Dupont", "[EMAIL_1]": "j@ex.com", …}

        Cible : < 150 ms pour 500 mots.
        """
        return await asyncio.get_running_loop().run_in_executor(
            self._executor,
            self._pseudonymize_sync,
            text,
            language,
            entities,
            score_threshold,
            excluded_entity_types,
        )

    async def smart_pseudonymize_detailed(
        self,
        text: str,
        language: Optional[str] = None,
        entities: Optional[List[str]] = None,
        score_threshold: float = 0.6,
        excluded_entity_types: Optional[List[str]] = None,
    ) -> PseudonymizationResult:
        return await asyncio.get_running_loop().run_in_executor(
            self._executor,
            self._pseudonymize_detailed_sync,
            text,
            language,
            entities,
            score_threshold,
            excluded_entity_types,
        )

    def reidentify(self, text: str, mapping: Dict[str, str]) -> str:
        """
        Remplace les tokens de pseudonymisation par leurs valeurs originales.

        Gère les variations de casse :
          [personne_1], [PERSONNE_1], [Personne_1] → tous restaurés.

        Args:
            text    : texte contenant des tokens [LABEL_N]
            mapping : dict retourné par smart_pseudonymize()

        Returns:
            texte avec toutes les valeurs PII restaurées.
        """
        if not mapping:
            return text

        for token, original in mapping.items():
            # re.escape gère les crochets et underscores
            # Lambda neutralise l'interpretation des backslashes dans le replacement
            text = re.sub(re.escape(token), lambda _m, _o=original: _o, text, flags=re.IGNORECASE)

        return text

    def get_entity_report(
        self,
        text: str,
        language: str = "fr",
        entities: Optional[List[str]] = None,
        score_threshold: float = 0.6,
        excluded_entity_types: Optional[List[str]] = None,
    ) -> dict:
        """
        Retourne la liste structurée des entités détectées (sans modifier le texte).
        Utile pour l'audit RGPD et l'affichage dans l'UI.
        """
        _, results = self._analyze_entities(
            text=text,
            language=language,
            entities=entities,
            score_threshold=score_threshold,
            excluded_entity_types=excluded_entity_types,
        )
        detailed = self._pseudonymize_detailed_sync(
            text=text,
            language=language,
            entities=entities,
            score_threshold=score_threshold,
            excluded_entity_types=excluded_entity_types,
        )
        return {
            "total": len(results),
            "by_type": {
                entity_type: len([r for r in results if r.entity_type == entity_type])
                for entity_type in set(r.entity_type for r in results)
            },
            "preview": [
                {
                    "type": r.entity_type,
                    "start": r.start,
                    "end": r.end,
                    "score": round(r.score, 2),
                }
                for r in results[:10]
            ],
            "explanation": detailed.explanation,
        }


# ---------------------------------------------------------------------------
# Helpers module-level
# ---------------------------------------------------------------------------

def _remove_overlaps(results: list) -> list:
    """
    Élimine les entités qui se chevauchent.
    En cas de chevauchement, conserve celle avec le score le plus élevé.
    Trie le résultat par position de début (croissant).
    """
    # Tri par début croissant, puis score décroissant (meilleur score en premier)
    sorted_results = sorted(results, key=lambda r: (r.start, -r.score))
    filtered: list = []
    last_end = -1
    for result in sorted_results:
        if result.start >= last_end:
            filtered.append(result)
            last_end = result.end
    return filtered



# ---------------------------------------------------------------------------
# Singleton partagé — lazy init, thread-safe
# Tous les modules doivent importer get_shared_redactor() plutôt que
# d'instancier Redactor() directement, pour éviter de charger les modèles
# spaCy plusieurs fois en mémoire.
# ---------------------------------------------------------------------------

import threading as _threading

_shared_instance: "Optional[Redactor]" = None
_shared_lock = _threading.Lock()
_shared_ready = _threading.Event()


def get_shared_redactor() -> "Redactor":
    """
    Retourne l'instance Redactor partagée (lazy init, double-checked lock).
    Premier appel : charge les modèles spaCy (~20-40s).
    Appels suivants : retour immédiat.
    """
    global _shared_instance
    if _shared_instance is None:
        with _shared_lock:
            if _shared_instance is None:
                _shared_instance = Redactor()
                _shared_ready.set()
    return _shared_instance


async def get_shared_redactor_async() -> "Redactor":
    """
    Retourne l'instance partagee sans bloquer la boucle asyncio pendant
    l'initialisation potentiellement lourde des modeles spaCy/Presidio.
    """
    return await asyncio.get_running_loop().run_in_executor(None, get_shared_redactor)

# ---------------------------------------------------------------------------
# Tests inline — python -m app.core.redactor
# Prérequis : SECRET_KEY et VAULT_ENCRYPTION_KEY dans l'environnement ou .env
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio
    import time

    TEST_CASES = [
        {
            "label": "🇫🇷 FR — Contrat professionnel complet",
            "text": (
                "Bonjour Jean Dupont, voici votre contrat signé le 15 mars 2024. "
                "Merci de nous contacter à jean.dupont@acme.fr ou au +33 6 12 34 56 78. "
                "Notre SIRET est 123 456 789 01234 et notre TVA FR12345678901. "
                "Virement IBAN FR76 3000 6000 0112 3456 7890 189. "
                "Adresse : 14 rue de la Paix, 75001 Paris. "
                "IP de connexion : 192.168.0.42."
            ),
        },
        {
            "label": "🇬🇧 EN — Medical record",
            "text": (
                "Patient John Smith (DOB: 15/03/1980) was admitted on 2024-01-10. "
                "Contact: john.smith@hospital.org, phone: +1 415-555-0198. "
                "Credit card: 4111 1111 1111 1111. "
                "Last login from IP 10.0.0.5. Profile: https://hospital.org/patients/jsmith."
            ),
        },
        {
            "label": "🇫🇷 FR — Déduplication (même personne × 3)",
            "text": (
                "Jean Dupont a signé le contrat. "
                "Nous attendons la réponse de Jean Dupont d'ici vendredi. "
                "Jean Dupont sera présent à la réunion du 20 mars."
            ),
        },
        {
            "label": "🇩🇪 DE — IBAN + TVA allemands",
            "text": (
                "Sehr geehrter Herr Müller, "
                "bitte überweisen Sie auf IBAN DE89 3704 0044 0532 0130 00. "
                "Ihre USt-IdNr. ist DE123456789. "
                "Kontakt: hans.mueller@firma.de"
            ),
        },
        {
            "label": "🇪🇸 ES — TVA espagnole",
            "text": (
                "Estimada María García, su número de IVA es ESX1234567A. "
                "Contacto: maria.garcia@empresa.es, tel: +34 612 345 678."
            ),
        },
    ]

    async def run_tests() -> None:
        print("\n" + "=" * 70)
        print("PRIVACY PROXY — Tests du moteur de pseudonymisation")
        print("=" * 70)

        redactor = Redactor()
        total_time = 0.0
        passed = 0
        failed = 0

        for case in TEST_CASES:
            print(f"\n{'─' * 70}")
            print(f"TEST : {case['label']}")
            print(f"Input : {case['text']}")

            t0 = time.perf_counter()
            pseudonymised, mapping = await redactor.smart_pseudonymize(case["text"])
            elapsed_ms = (time.perf_counter() - t0) * 1000
            total_time += elapsed_ms

            print(f"Output : {pseudonymised}")
            print(f"Mapping : {mapping}")
            print(f"Temps   : {elapsed_ms:.1f} ms")

            # ── Vérification réidentification ──────────────────────────────
            restored = redactor.reidentify(pseudonymised, mapping)
            if restored == case["text"]:
                print("Restauration : ✅ OK")
                passed += 1
            else:
                print(f"Restauration : ❌ ÉCHEC")
                print(f"  Attendu : {case['text']}")
                print(f"  Obtenu  : {restored}")
                failed += 1

            # ── Vérification déduplication ─────────────────────────────────
            for token, original in mapping.items():
                n_in = case["text"].count(original)
                n_out = pseudonymised.count(token)
                if n_in > 1 and n_out != n_in:
                    print(
                        f"  ⚠️  Dédup '{original}': "
                        f"{n_in}× en entrée, {n_out}× en sortie"
                    )

            # ── Vérification performance ───────────────────────────────────
            if elapsed_ms > 150:
                print(f"  ⚠️  Lent : {elapsed_ms:.0f} ms > 150 ms cible")

        print(f"\n{'=' * 70}")
        print(
            f"Résultats : {passed}/{len(TEST_CASES)} tests OK | "
            f"Temps moyen : {total_time / len(TEST_CASES):.1f} ms/req"
        )
        if failed:
            print(f"  ❌ {failed} test(s) échoué(s) — voir détails ci-dessus")

    asyncio.run(run_tests())
