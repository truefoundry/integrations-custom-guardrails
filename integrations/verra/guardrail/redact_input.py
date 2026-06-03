"""Input redact rail: mask PII and secrets in the prompt via Verra."""

from __future__ import annotations

from entities import InputGuardrailRequest, MutateGuardrailResponse
from guardrail._verra_client import call_verra


def redact_input(request: InputGuardrailRequest) -> MutateGuardrailResponse:
    result = call_verra("input/redact", request.model_dump(exclude_none=True))
    return MutateGuardrailResponse(**result)
