"""Input rail: Guardrails AI ToxicLanguage validator on the user message."""

from guardrails import Guard
from guardrails.hub import ToxicLanguage

from entities import InputGuardrailRequest, ValidateGuardrailResponse
from guardrail._helpers import last_user_text

guard = Guard().use(ToxicLanguage(threshold=0.5, on_fail="exception"))


def toxic_language_input(request: InputGuardrailRequest) -> ValidateGuardrailResponse:
    user_msg = last_user_text(request.requestBody.get("messages") or [])
    if user_msg is None:
        return ValidateGuardrailResponse(verdict=True)
    try:
        guard.validate(user_msg)
        return ValidateGuardrailResponse(verdict=True)
    except Exception as e:
        return ValidateGuardrailResponse(
            verdict=False, message=f"ToxicLanguage (input): {str(e)[:300]}"
        )
