"""Output rail: Guardrails AI ToxicLanguage validator on the assistant response."""

from guardrails import Guard
from guardrails.hub import ToxicLanguage

from entities import OutputGuardrailRequest, ValidateGuardrailResponse
from guardrail._helpers import first_assistant_text

guard = Guard().use(ToxicLanguage(threshold=0.5, on_fail="exception"))


def toxic_language_output(request: OutputGuardrailRequest) -> ValidateGuardrailResponse:
    assistant_msg = first_assistant_text(request.responseBody.get("choices") or [])
    if assistant_msg is None:
        return ValidateGuardrailResponse(verdict=True)
    try:
        guard.validate(assistant_msg)
        return ValidateGuardrailResponse(verdict=True)
    except Exception as e:
        return ValidateGuardrailResponse(
            verdict=False, message=f"ToxicLanguage (output): {str(e)[:300]}"
        )
