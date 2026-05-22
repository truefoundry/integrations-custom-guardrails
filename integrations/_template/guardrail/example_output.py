"""Example output rail. Replace with your vendor's actual validator logic."""

from entities import OutputGuardrailRequest, ValidateGuardrailResponse
from guardrail._helpers import first_assistant_text


def example_output(request: OutputGuardrailRequest) -> ValidateGuardrailResponse:
    assistant_msg = first_assistant_text(request.responseBody.get("choices") or [])
    if assistant_msg is None:
        return ValidateGuardrailResponse(verdict=True)

    # TODO: call your vendor here.
    # try:
    #     vendor.validate(assistant_msg)
    #     return ValidateGuardrailResponse(verdict=True)
    # except VendorViolation as e:
    #     return ValidateGuardrailResponse(
    #         verdict=False,
    #         message=f"<Vendor> (output): {str(e)[:300]}",
    #     )

    return ValidateGuardrailResponse(verdict=True)
