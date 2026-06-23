"""HiddenLayer validate rails via v2 interaction-evaluations."""

from __future__ import annotations

from entities import InputGuardrailRequest, OutputGuardrailRequest, ValidateGuardrailResponse
from guardrail._helpers import (
    build_interaction_evaluations_payload,
    has_scannable_input_messages,
    has_scannable_output,
    resolve_session_id,
    tf_messages_to_v2,
)
from guardrail._hiddenlayer_client import (
    HiddenLayerApiError,
    call_interaction_evaluations,
    check_inline_validate_enforcement,
    handle_hiddenlayer_error,
    map_validate_response,
    resolve_fail_open_on_unavailable,
    resolve_provider,
)


def validate_input(request: InputGuardrailRequest) -> ValidateGuardrailResponse:
    """Validate user input via HiddenLayer v2 interaction + inline request-evaluations."""
    try:
        messages = request.requestBody.get("messages") or []
        if not isinstance(messages, list) or not has_scannable_input_messages(messages):
            return ValidateGuardrailResponse(verdict=True)

        v2_messages = tf_messages_to_v2(messages)
        if not v2_messages:
            return ValidateGuardrailResponse(verdict=True)

        session_id = resolve_session_id(request.config, request.context)
        payload = build_interaction_evaluations_payload(
            request_body=request.requestBody,
            config=request.config,
            context=request.context,
            provider=resolve_provider(request.config),
        )
        hl_response = call_interaction_evaluations(payload, request.config)
        allowed, message, action = map_validate_response(
            hl_response,
            original_messages=v2_messages,
            config=request.config,
        )
        if allowed and action == "NONE":
            inline_ok, inline_msg = check_inline_validate_enforcement(
                original_body=request.requestBody,
                config=request.config,
                session_id=session_id,
                phase="input",
            )
            if not inline_ok:
                return ValidateGuardrailResponse(verdict=False, message=inline_msg)
        if allowed:
            return ValidateGuardrailResponse(verdict=True)
        return ValidateGuardrailResponse(verdict=False, message=message)
    except HiddenLayerApiError as exc:
        if exc.status_code >= 500 and resolve_fail_open_on_unavailable(request.config):
            return ValidateGuardrailResponse(verdict=True)
        handle_hiddenlayer_error(exc)
    except Exception as exc:
        handle_hiddenlayer_error(exc)


def validate_output(request: OutputGuardrailRequest) -> ValidateGuardrailResponse:
    """Validate model output via HiddenLayer v2 interaction + inline response-evaluations."""
    try:
        choices = request.responseBody.get("choices") or []
        if not isinstance(choices, list) or not has_scannable_output(choices):
            return ValidateGuardrailResponse(verdict=True)

        session_id = resolve_session_id(request.config, request.context)
        payload = build_interaction_evaluations_payload(
            request_body=request.requestBody,
            config=request.config,
            context=request.context,
            provider=resolve_provider(request.config),
            response_body=request.responseBody,
        )
        original_messages = payload["interaction"]["messages"]
        if not original_messages:
            return ValidateGuardrailResponse(verdict=True)

        hl_response = call_interaction_evaluations(payload, request.config)
        allowed, message, action = map_validate_response(
            hl_response,
            original_messages=original_messages,
            config=request.config,
        )
        if allowed and action == "NONE":
            inline_ok, inline_msg = check_inline_validate_enforcement(
                original_body=request.responseBody,
                config=request.config,
                session_id=session_id,
                phase="output",
            )
            if not inline_ok:
                return ValidateGuardrailResponse(verdict=False, message=inline_msg)
        if allowed:
            return ValidateGuardrailResponse(verdict=True)
        return ValidateGuardrailResponse(verdict=False, message=message)
    except HiddenLayerApiError as exc:
        if exc.status_code >= 500 and resolve_fail_open_on_unavailable(request.config):
            return ValidateGuardrailResponse(verdict=True)
        handle_hiddenlayer_error(exc)
    except Exception as exc:
        handle_hiddenlayer_error(exc)
