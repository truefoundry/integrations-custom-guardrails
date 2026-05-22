"""Input rail: Guardrails AI DetectPII validator on the user message."""

from guardrails import Guard
from guardrails.hub import DetectPII

from entities import InputGuardrailRequest, ValidateGuardrailResponse
from guardrail._helpers import last_user_text
from guardrail._pii_entities import PII_ENTITIES

# Instance-form `on_fail` is required on guardrails-ai v0.9.3+.
# Class-form (.use(DetectPII, on_fail="exception")) raises TypeError.
guard = Guard().use(DetectPII(pii_entities=PII_ENTITIES, on_fail="exception"))


def detect_pii_input(request: InputGuardrailRequest) -> ValidateGuardrailResponse:
    user_msg = last_user_text(request.requestBody.get("messages") or [])
    if user_msg is None:
        return ValidateGuardrailResponse(verdict=True)
    try:
        guard.validate(user_msg)
        return ValidateGuardrailResponse(verdict=True)
    except Exception as e:
        return ValidateGuardrailResponse(
            verdict=False, message=f"DetectPII (input): {str(e)[:300]}"
        )
