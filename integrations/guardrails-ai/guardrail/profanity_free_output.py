"""Output rail: Guardrails AI ProfanityFree validator on the assistant response.

Output-only by design — profanity in the user's own input is the user's choice,
but we don't want the assistant to produce explicit language.
"""

from guardrails import Guard
from guardrails.hub import ProfanityFree

from entities import OutputGuardrailRequest, ValidateGuardrailResponse
from guardrail._helpers import first_assistant_text

guard = Guard().use(ProfanityFree(on_fail="exception"))


def profanity_free_output(request: OutputGuardrailRequest) -> ValidateGuardrailResponse:
    assistant_msg = first_assistant_text(request.responseBody.get("choices") or [])
    if assistant_msg is None:
        return ValidateGuardrailResponse(verdict=True)
    try:
        guard.validate(assistant_msg)
        return ValidateGuardrailResponse(verdict=True)
    except Exception as e:
        return ValidateGuardrailResponse(
            verdict=False, message=f"ProfanityFree (output): {str(e)[:300]}"
        )
