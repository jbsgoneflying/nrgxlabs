"""Raven Chat — streaming SSE endpoint for the Senior Quant Trader advisor."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

LOG = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/chat")
async def raven_chat_stream(request: Request):
    """Streaming chat endpoint.

    Accepts: { messages: [{role, content}], engineId?: str, engineData?: dict }
    Returns: SSE stream with data: {"chunk":"..."} events, ending with {"done":true}.
    """
    from backend.config import get_flags
    flags = get_flags()

    if not getattr(flags, "ENABLE_RAVEN_CHAT", True):
        raise HTTPException(status_code=503, detail="NRGX Chat is disabled")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages array is required")

    valid_roles = {"user", "assistant"}
    sanitized = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if role in valid_roles and isinstance(content, str) and content.strip():
            sanitized.append({"role": role, "content": content.strip()})

    if not sanitized:
        raise HTTPException(status_code=400, detail="No valid messages provided")

    engine_id = body.get("engineId")
    engine_data = body.get("engineData")

    from backend.raven_chat import build_chat_context, stream_chat_response

    context = build_chat_context(engine_id, engine_data, flags=flags)

    def event_stream():
        yield from stream_chat_response(sanitized, context, flags=flags)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
