"""Onyx AI Guard - output rail (validate).

Runs on the llm_output hook. Sends extracted assistant text as ``response`` to
Onyx /simple, and maps action allow → verdict true; block/modify → verdict false.
"""

from __future__ import annotations

from fastapi import HTTPException

from entities import OutputGuardrailRequest, ValidateGuardrailResponse
from guardrail._helpers import first_assistant_text
from guardrail._onyx_client import (
    evaluate,
    format_block_message,
    is_allow,
    resolve_settings,
)


async def onyx_output(request: OutputGuardrailRequest) -> ValidateGuardrailResponse:
    choices = request.responseBody.get("choices") or []
    assistant_msg = first_assistant_text(choices)

    # Short-circuit: no assistant content to check -> allow without calling Onyx.
    if assistant_msg is None:
        return ValidateGuardrailResponse(verdict=True)

    api_key, api_base, timeout = resolve_settings(request.config)
    if not api_key:
        raise HTTPException(status_code=500, detail="Onyx API key not configured")

    try:
        result = await evaluate(
            api_base=api_base,
            api_key=api_key,
            text=assistant_msg,
            direction="output",
            timeout=timeout,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Onyx AI Guard call failed: {e}")

    if is_allow(result.action):
        return ValidateGuardrailResponse(verdict=True)

    # block, and modify (fail-safe: validate rails cannot apply masking)
    return ValidateGuardrailResponse(
        verdict=False,
        message=format_block_message("output", result.custom_popup_message),
    )
