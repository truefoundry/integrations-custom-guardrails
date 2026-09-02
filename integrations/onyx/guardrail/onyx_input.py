"""Onyx AI Guard - input rail (validate).

Runs on the llm_input hook. Sends extracted user text as ``user_prompt`` to
Onyx /simple, and maps action allow → verdict true; block/modify → verdict false.
"""

from __future__ import annotations

from fastapi import HTTPException

from entities import InputGuardrailRequest, ValidateGuardrailResponse
from guardrail._helpers import last_user_text
from guardrail._onyx_client import (
    OnyxClientError,
    evaluate,
    format_block_message,
    is_allow,
    resolve_settings,
)


async def onyx_input(request: InputGuardrailRequest) -> ValidateGuardrailResponse:
    messages = request.requestBody.get("messages") or []
    user_msg = last_user_text(messages)

    # Short-circuit: nothing user-authored to check -> allow without calling Onyx.
    if user_msg is None:
        return ValidateGuardrailResponse(verdict=True)

    api_key, api_base, timeout = resolve_settings(request.config)
    if not api_key:
        # Misconfiguration is a real error, not a policy decision -> 5xx.
        raise HTTPException(status_code=500, detail="Onyx API key not configured")
    if not api_base:
        raise HTTPException(status_code=500, detail="Onyx API base not configured")

    try:
        result = await evaluate(
            api_base=api_base,
            api_key=api_key,
            text=user_msg,
            direction="input",
            timeout=timeout,
        )
    except OnyxClientError as e:
        # Real outage/error -> 5xx so the dashboard's `Fail on error` policy decides.
        # OnyxClientError messages are URL-safe (no policy token).
        raise HTTPException(status_code=502, detail=f"Onyx AI Guard call failed: {e}")
    except Exception:
        # Unexpected failures must not interpolate raw exception text — httpx (and
        # similar) can embed the evaluate URL, which contains ONYX_API_KEY.
        raise HTTPException(status_code=502, detail="Onyx AI Guard call failed")

    if is_allow(result.action):
        return ValidateGuardrailResponse(verdict=True)

    # block, and modify (fail-safe: validate rails cannot apply masking)
    return ValidateGuardrailResponse(
        verdict=False,
        message=format_block_message("input", result.custom_popup_message),
    )
