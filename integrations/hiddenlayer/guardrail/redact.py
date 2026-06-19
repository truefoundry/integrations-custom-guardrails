"""HiddenLayer redact rails (input and output) via v1/interactions."""

from __future__ import annotations

from typing import Any, Optional

from entities import InputGuardrailRequest, MutateGuardrailResponse, OutputGuardrailRequest
from guardrail._helpers import (
    apply_hl_input_to_request_body,
    apply_hl_output_to_response_body,
    clone_body,
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
    map_redact_response,
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


def _pass_through(body: dict[str, Any]) -> MutateGuardrailResponse:
    return MutateGuardrailResponse(verdict=True, transformed=False, result=clone_body(body))


def redact_input(request: InputGuardrailRequest) -> MutateGuardrailResponse:
    """Redact or block user input via HiddenLayer POST /detection/v1/interactions."""
    try:
        messages = request.requestBody.get("messages") or []
        if not isinstance(messages, list) or not has_scannable_input_messages(messages):
            return _pass_through(request.requestBody)

        hl_messages, source_indices = tf_messages_to_hl(messages)
        if not hl_messages:
            return _pass_through(request.requestBody)

        payload = build_interactions_payload(
            metadata=_build_metadata(request.requestBody, request.config, request.context),
            input_messages=hl_messages,
        )
        hl_response = call_hiddenlayer_interactions(
            payload,
            request.config,
            session_id=resolve_session_id(request.config, request.context),
        )
        modified_data = hl_response.get("modified_data") or {}
        modified_input = modified_data.get("input") or {"messages": hl_messages}
        modified_body = apply_hl_input_to_request_body(
            request.requestBody,
            modified_input,
            source_indices,
        )

        allowed, transformed, result_body, _ = map_redact_response(
            hl_response,
            original_body=request.requestBody,
            modified_body=modified_body,
        )
        return MutateGuardrailResponse(
            verdict=allowed,
            transformed=transformed if allowed else False,
            result=clone_body(result_body),
        )
    except HiddenLayerApiError as exc:
        if exc.status_code >= 500 and resolve_fail_open_on_unavailable(request.config):
            return _pass_through(request.requestBody)
        handle_hiddenlayer_error(exc)
    except Exception as exc:
        handle_hiddenlayer_error(exc)


def redact_output(request: OutputGuardrailRequest) -> MutateGuardrailResponse:
    """Redact or block model output via HiddenLayer POST /detection/v1/interactions."""
    try:
        choices = request.responseBody.get("choices") or []
        if not isinstance(choices, list) or not has_scannable_output(choices):
            return _pass_through(request.responseBody)

        hl_messages, source_indices = tf_choices_to_hl_output(choices)
        if not hl_messages:
            return _pass_through(request.responseBody)

        payload = build_interactions_payload(
            metadata=_build_metadata(request.requestBody, request.config, request.context),
            output_messages=hl_messages,
        )
        hl_response = call_hiddenlayer_interactions(
            payload,
            request.config,
            session_id=resolve_session_id(request.config, request.context),
        )
        modified_data = hl_response.get("modified_data") or {}
        modified_output = modified_data.get("output") or {"messages": hl_messages}
        modified_body = apply_hl_output_to_response_body(
            request.responseBody,
            modified_output,
            source_indices,
        )

        allowed, transformed, result_body, _ = map_redact_response(
            hl_response,
            original_body=request.responseBody,
            modified_body=modified_body,
        )
        return MutateGuardrailResponse(
            verdict=allowed,
            transformed=transformed if allowed else False,
            result=clone_body(result_body),
        )
    except HiddenLayerApiError as exc:
        if exc.status_code >= 500 and resolve_fail_open_on_unavailable(request.config):
            return _pass_through(request.responseBody)
        handle_hiddenlayer_error(exc)
    except Exception as exc:
        handle_hiddenlayer_error(exc)
