"""Onyx AI Guard - output rail (validate).

Forwards the TrueFoundry output payload (``requestBody`` + ``responseBody``) to
Onyx ``/truefoundry`` and returns Onyx's ``verdict`` / ``message`` unchanged.
Prefer pointing the gateway at Onyx directly in production.
"""

from __future__ import annotations

from fastapi import HTTPException

from entities import OutputGuardrailRequest, ValidateGuardrailResponse
from guardrail._helpers import first_assistant_text
from guardrail._onyx_client import OnyxClientError, evaluate, resolve_settings


async def onyx_output(request: OutputGuardrailRequest) -> ValidateGuardrailResponse:
    choices = request.responseBody.get("choices") or []

    # Short-circuit: no assistant content to check -> allow without calling Onyx.
    if first_assistant_text(choices) is None:
        return ValidateGuardrailResponse(verdict=True)

    api_key, api_base, timeout = resolve_settings(request.config)
    if not api_key:
        raise HTTPException(status_code=500, detail="Onyx API key not configured")
    if not api_base:
        raise HTTPException(status_code=500, detail="Onyx API base not configured")

    payload = {
        "requestBody": request.requestBody,
        "responseBody": request.responseBody,
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
