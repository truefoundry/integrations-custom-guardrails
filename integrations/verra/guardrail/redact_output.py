"""Output redact rail: mask PII and secrets in the model's response via Verra."""

from __future__ import annotations

from entities import OutputGuardrailRequest, MutateGuardrailResponse
from guardrail._verra_client import call_verra


def redact_output(request: OutputGuardrailRequest) -> MutateGuardrailResponse:
    result = call_verra("output/redact", request.model_dump(exclude_none=True))
    return MutateGuardrailResponse(**result)
