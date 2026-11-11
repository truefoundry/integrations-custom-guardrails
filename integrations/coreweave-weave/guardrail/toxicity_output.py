"""Output rail: CoreWeave Weave toxicity scorer (Celadon) on the assistant response."""

from __future__ import annotations

from entities import OutputGuardrailRequest, ValidateGuardrailResponse
from guardrail._helpers import first_assistant_text
from guardrail._weave_runner import evaluate


def toxicity_output(request: OutputGuardrailRequest) -> ValidateGuardrailResponse:
    assistant_msg = first_assistant_text(request.responseBody.get("choices") or [])
    if assistant_msg is None:
        return ValidateGuardrailResponse(verdict=True)

    r = evaluate(assistant_msg, request.config)
    if r["passed"]:
        return ValidateGuardrailResponse(verdict=True)

    return ValidateGuardrailResponse(
        verdict=False,
        message=(
            f"WeaveToxicity (output): blocked on {r['top_category']} "
            f"(score={r['top_score']}, total={r['total']}, "
            f"thresholds={r['thresholds']})"
        ),
    )
