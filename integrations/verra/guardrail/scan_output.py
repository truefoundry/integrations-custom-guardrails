"""Output scan rail: validate the model's response via Verra (secrets, policy violations)."""

from __future__ import annotations

from entities import OutputGuardrailRequest, ValidateGuardrailResponse
from guardrail._verra_client import call_verra


def scan_output(request: OutputGuardrailRequest) -> ValidateGuardrailResponse:
    result = call_verra("output/scan", request.model_dump(exclude_none=True))
    return ValidateGuardrailResponse(**result)
