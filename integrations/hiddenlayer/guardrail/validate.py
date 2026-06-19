"""HiddenLayer validate rails (input and output) via v1/interactions."""

from __future__ import annotations

from typing import Any, Optional

from entities import InputGuardrailRequest, OutputGuardrailRequest, ValidateGuardrailResponse
from guardrail._helpers import (
    has_scannable_input_messages,
    has_scannable_output,
    resolve_requester_id,
    resolve_session_id,
    tf_choices_to_hl_output,
    tf_messages_to_hl,
)
from guardrail._hiddenlayer_client import (
    HiddenLayerApiError,
    build_interactions_payload,
    call_hiddenlayer_interactions,
    handle_hiddenlayer_error,
    map_validate_response,
    resolve_fail_open_on_unavailable,
    resolve_provider,
)


def _build_metadata(
    request_body: dict[str, Any],
    config: Optional[dict],
    context: Any,
) -> dict[str, Any]:
    model = str(request_body.get("model") or "unknown")
    return {
        "model": model,
        "requester_id": resolve_requester_id(config, context),
        "provider": resolve_provider(config),
    }


def validate_input(request: InputGuardrailRequest) -> ValidateGuardrailResponse:
    """Validate user input via HiddenLayer POST /detection/v1/interactions."""
    try:
        messages = request.requestBody.get("messages") or []
        if not isinstance(messages, list) or not has_scannable_input_messages(messages):
            return ValidateGuardrailResponse(verdict=True)

        hl_messages, _ = tf_messages_to_hl(messages)
        if not hl_messages:
            return ValidateGuardrailResponse(verdict=True)

        payload = build_interactions_payload(
            metadata=_build_metadata(request.requestBody, request.config, request.context),
            input_messages=hl_messages,
        )
        hl_response = call_hiddenlayer_interactions(
            payload,
            request.config,
            session_id=resolve_session_id(request.config, request.context),
        )
        allowed, message = map_validate_response(hl_response)
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
    """Validate model output via HiddenLayer POST /detection/v1/interactions."""
    try:
        choices = request.responseBody.get("choices") or []
        if not isinstance(choices, list) or not has_scannable_output(choices):
            return ValidateGuardrailResponse(verdict=True)

        hl_messages, _ = tf_choices_to_hl_output(choices)
        if not hl_messages:
            return ValidateGuardrailResponse(verdict=True)

        payload = build_interactions_payload(
            metadata=_build_metadata(request.requestBody, request.config, request.context),
            output_messages=hl_messages,
        )
        hl_response = call_hiddenlayer_interactions(
            payload,
            request.config,
            session_id=resolve_session_id(request.config, request.context),
        )
        allowed, message = map_validate_response(hl_response)
        if allowed:
            return ValidateGuardrailResponse(verdict=True)
        return ValidateGuardrailResponse(verdict=False, message=message)
    except HiddenLayerApiError as exc:
        if exc.status_code >= 500 and resolve_fail_open_on_unavailable(request.config):
            return ValidateGuardrailResponse(verdict=True)
        handle_hiddenlayer_error(exc)
    except Exception as exc:
        handle_hiddenlayer_error(exc)
