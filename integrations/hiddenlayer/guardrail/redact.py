"""HiddenLayer mutate rails via v2 inline request/response-evaluations."""

from __future__ import annotations

from entities import InputGuardrailRequest, MutateGuardrailResponse, OutputGuardrailRequest
from guardrail._helpers import (
    clone_body,
    has_scannable_input_messages,
    has_scannable_output,
    resolve_session_id,
)
from guardrail._hiddenlayer_client import (
    HiddenLayerApiError,
    call_request_evaluations,
    call_response_evaluations,
    handle_hiddenlayer_error,
    map_inline_mutate_response,
    resolve_fail_open_on_unavailable,
)


def _pass_through(body: dict) -> MutateGuardrailResponse:
    return MutateGuardrailResponse(verdict=True, transformed=False, result=clone_body(body))


def redact_input(request: InputGuardrailRequest) -> MutateGuardrailResponse:
    """Redact or block user input via HiddenLayer POST /detection/v2/request-evaluations."""
    try:
        messages = request.requestBody.get("messages") or []
        if not isinstance(messages, list) or not has_scannable_input_messages(messages):
            return _pass_through(request.requestBody)

        result = call_request_evaluations(
            request.requestBody,
            request.config,
            session_id=resolve_session_id(request.config, request.context),
        )
        allowed, transformed, result_body, _ = map_inline_mutate_response(
            result,
            original_body=request.requestBody,
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
    """Redact or block model output via HiddenLayer POST /detection/v2/response-evaluations."""
    try:
        choices = request.responseBody.get("choices") or []
        if not isinstance(choices, list) or not has_scannable_output(choices):
            return _pass_through(request.responseBody)

        result = call_response_evaluations(
            request.responseBody,
            request.config,
            session_id=resolve_session_id(request.config, request.context),
        )
        allowed, transformed, result_body, _ = map_inline_mutate_response(
            result,
            original_body=request.responseBody,
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
