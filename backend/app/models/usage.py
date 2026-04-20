"""
Modèles Pydantic pour le tracking d'usage LLM.

UsageRecord  : un enregistrement par requête (chat/upload/stream)
MonthlyUsage : agrégat mensuel par utilisateur
"""

from typing import Dict, Literal
from pydantic import BaseModel


class UsageRecord(BaseModel):
    user_id: str
    session_id_hash: str          # SHA-256 du session_id — aucune PII exposée
    model_used: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    timestamp: str                # ISO 8601 UTC
    request_type: Literal["chat", "upload", "stream"]
    org_id: str = ""


class MonthlyUsage(BaseModel):
    user_id: str
    month: str                    # YYYY-MM
    total_tokens: int
    total_cost_usd: float
    requests_count: int
    breakdown_by_provider: Dict[str, Dict]   # {provider: {tokens, cost_usd, count}}
    breakdown_by_model: Dict[str, Dict]      # {model:    {tokens, cost_usd, count}}
