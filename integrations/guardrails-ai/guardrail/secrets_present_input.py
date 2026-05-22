"""Input rail: Guardrails AI SecretsPresent validator on the user message."""

from guardrails import Guard
from guardrails.hub import SecretsPresent

from entities import InputGuardrailRequest, ValidateGuardrailResponse
from guardrail._helpers import last_user_text

guard = Guard().use(SecretsPresent(on_fail="exception"))


def secrets_present_input(request: InputGuardrailRequest) -> ValidateGuardrailResponse:
    user_msg = last_user_text(request.requestBody.get("messages") or [])
    if user_msg is None:
        return ValidateGuardrailResponse(verdict=True)
    try:
        guard.validate(user_msg)
        return ValidateGuardrailResponse(verdict=True)
    except Exception as e:
        return ValidateGuardrailResponse(
            verdict=False, message=f"SecretsPresent (input): {str(e)[:300]}"
        )
