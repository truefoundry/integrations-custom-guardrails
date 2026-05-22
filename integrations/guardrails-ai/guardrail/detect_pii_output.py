"""Output rail: Guardrails AI DetectPII validator on the assistant response."""

from guardrails import Guard
from guardrails.hub import DetectPII

from entities import OutputGuardrailRequest, ValidateGuardrailResponse
from guardrail._helpers import first_assistant_text
from guardrail._pii_entities import PII_ENTITIES

guard = Guard().use(DetectPII(pii_entities=PII_ENTITIES, on_fail="exception"))


def detect_pii_output(request: OutputGuardrailRequest) -> ValidateGuardrailResponse:
    assistant_msg = first_assistant_text(request.responseBody.get("choices") or [])
    if assistant_msg is None:
        return ValidateGuardrailResponse(verdict=True)
    try:
        guard.validate(assistant_msg)
        return ValidateGuardrailResponse(verdict=True)
    except Exception as e:
        return ValidateGuardrailResponse(
            verdict=False, message=f"DetectPII (output): {str(e)[:300]}"
        )
