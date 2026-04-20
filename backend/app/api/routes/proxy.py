"""
Proxy routes legacy — DESACTIVEES.

Ces routes (/api/proxy/chat, /api/proxy/chat/stream, /api/proxy/upload)
sont remplacees par les routes modernes :
  - POST /api/chat/stream   (SSE, authentifie, vault namespaced)
  - POST /api/upload        (authentifie, vault namespaced)

Toutes les routes ci-dessous retournent HTTP 410 Gone.
"""

from fastapi import APIRouter, HTTPException

router = APIRouter()


async def _legacy_gone():
    raise HTTPException(
        status_code=410,
        detail=(
            "Cette route est desactivee. "
            "Utilisez /api/chat/stream et /api/upload."
        ),
    )


@router.post("/chat")
async def legacy_chat():
    await _legacy_gone()


@router.post("/chat/stream")
async def legacy_chat_stream():
    await _legacy_gone()


@router.post("/upload")
async def legacy_upload():
    await _legacy_gone()
