"""Arthur GenAI Engine validate rails (input and output)."""

from __future__ import annotations

from typing import Optional

from entities import InputGuardrailRequest, OutputGuardrailRequest, ValidateGuardrailResponse
from guardrail._arthur_client import (
    build_validate_payload,
    call_arthur_validate,
    handle_arthur_error,
    map_arthur_response,
    resolve_checks,
    resolve_fail_closed_on_unavailable,
)
from guardrail._helpers import first_assistant_text, last_user_text, system_context_text


def _resolve_context(config: Optional[dict], request_body: dict) -> Optional[str]:
    if config:
        for key in ("context", "grounding_context"):
            value = config.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    messages = request_body.get("messages") or []
    if isinstance(messages, list):
        return system_context_text(messages)
    return None


def validate_input(request: InputGuardrailRequest) -> ValidateGuardrailResponse:
    """Validate user input via Arthur POST /api/v2/validate."""
    try:
        messages = request.requestBody.get("messages") or []
        prompt = last_user_text(messages if isinstance(messages, list) else [])
        if prompt is None:
            return ValidateGuardrailResponse(verdict=True)

        checks = resolve_checks(request.config, for_prompt=True)
        context = _resolve_context(request.config, request.requestBody)
        payload = build_validate_payload(checks=checks, prompt=prompt, context=context)
        arthur_response = call_arthur_validate(payload, request.config)
        allowed, message = map_arthur_response(
            arthur_response,
            fail_closed_on_unavailable=resolve_fail_closed_on_unavailable(request.config),
        )
        if allowed:
            return ValidateGuardrailResponse(verdict=True)
        return ValidateGuardrailResponse(verdict=False, message=message)
    except Exception as exc:
        handle_arthur_error(exc)


def validate_output(request: OutputGuardrailRequest) -> ValidateGuardrailResponse:
    """Validate model output via Arthur POST /api/v2/validate."""
    try:
        choices = request.responseBody.get("choices") or []
        response_text = first_assistant_text(choices if isinstance(choices, list) else [])
        if response_text is None:
            return ValidateGuardrailResponse(verdict=True)

        checks = resolve_checks(request.config, for_prompt=False)
        context = _resolve_context(request.config, request.requestBody)
        payload = build_validate_payload(
            checks=checks,
            response=response_text,
            context=context,
        )
        arthur_response = call_arthur_validate(payload, request.config)
        allowed, message = map_arthur_response(
            arthur_response,
            fail_closed_on_unavailable=resolve_fail_closed_on_unavailable(request.config),
        )
        if allowed:
            return ValidateGuardrailResponse(verdict=True)
        return ValidateGuardrailResponse(verdict=False, message=message)
    except Exception as exc:
        handle_arthur_error(exc)
