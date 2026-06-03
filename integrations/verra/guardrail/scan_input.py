"""Input scan rail: validate user input via Verra (injection, jailbreak, exfiltration, policy)."""

from __future__ import annotations

from entities import InputGuardrailRequest, ValidateGuardrailResponse
from guardrail._verra_client import call_verra


def scan_input(request: InputGuardrailRequest) -> ValidateGuardrailResponse:
    result = call_verra("input/scan", request.model_dump(exclude_none=True))
    return ValidateGuardrailResponse(**result)
