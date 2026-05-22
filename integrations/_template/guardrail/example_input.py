"""Example input rail. Replace with your vendor's actual validator logic.

Pattern: extract the last user message, run validation, translate the outcome
to ValidateGuardrailResponse. Returns 200 + JSON; never raise for policy decisions.
"""

from entities import InputGuardrailRequest, ValidateGuardrailResponse
from guardrail._helpers import last_user_text


def example_input(request: InputGuardrailRequest) -> ValidateGuardrailResponse:
    user_msg = last_user_text(request.requestBody.get("messages") or [])
    if user_msg is None:
        return ValidateGuardrailResponse(verdict=True)

    # TODO: call your vendor here.
    # try:
    #     vendor.validate(user_msg)
    #     return ValidateGuardrailResponse(verdict=True)
    # except VendorViolation as e:
    #     return ValidateGuardrailResponse(
    #         verdict=False,
    #         message=f"<Vendor> (input): {str(e)[:300]}",
    #     )

    # Placeholder: stub allow-all until vendor logic is wired up.
    return ValidateGuardrailResponse(verdict=True)
