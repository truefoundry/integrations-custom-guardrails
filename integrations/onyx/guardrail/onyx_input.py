"""Onyx AI Guard - input rail (validate).

Forwards the TrueFoundry input payload to Onyx ``/truefoundry`` and returns
Onyx's ``verdict`` / ``message`` unchanged. Prefer pointing the gateway at
Onyx directly in production; this handler is for local/wrapper use.
"""

from __future__ import annotations

from fastapi import HTTPException

from entities import InputGuardrailRequest, ValidateGuardrailResponse
from guardrail._helpers import last_user_text
from guardrail._onyx_client import OnyxClientError, evaluate, resolve_settings


async def onyx_input(request: InputGuardrailRequest) -> ValidateGuardrailResponse:
    messages = request.requestBody.get("messages") or []

    # Short-circuit: nothing user-authored to check -> allow without calling Onyx.
    # Matches Onyx /truefoundry behavior for empty user text.
    if last_user_text(messages) is None:
        return ValidateGuardrailResponse(verdict=True)

    api_key, api_base, timeout = resolve_settings(request.config)
    if not api_key:
        raise HTTPException(status_code=500, detail="Onyx API key not configured")
    if not api_base:
        raise HTTPException(status_code=500, detail="Onyx API base not configured")

    payload = {
        "requestBody": request.requestBody,
        "context": request.context.model_dump(),
        "config": request.config or {},
    }

    try:
        result = await evaluate(
            api_base=api_base,
            api_key=api_key,
            payload=payload,
            timeout=timeout,
        )
    except OnyxClientError as e:
        raise HTTPException(status_code=502, detail=f"Onyx AI Guard call failed: {e}")
    except Exception:
        raise HTTPException(status_code=502, detail="Onyx AI Guard call failed")

    return ValidateGuardrailResponse(verdict=result.verdict, message=result.message)
