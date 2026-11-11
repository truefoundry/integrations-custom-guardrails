"""Input rail: CoreWeave Weave toxicity scorer (Celadon) on the user message."""

from __future__ import annotations

from entities import InputGuardrailRequest, ValidateGuardrailResponse
from guardrail._helpers import last_user_text
from guardrail._weave_runner import evaluate


def toxicity_input(request: InputGuardrailRequest) -> ValidateGuardrailResponse:
    user_msg = last_user_text(request.requestBody.get("messages") or [])
    if user_msg is None:
        return ValidateGuardrailResponse(verdict=True)

    r = evaluate(user_msg, request.config)
    if r["passed"]:
        return ValidateGuardrailResponse(verdict=True)

    return ValidateGuardrailResponse(
        verdict=False,
        message=(
            f"WeaveToxicity (input): blocked on {r['top_category']} "
            f"(score={r['top_score']}, total={r['total']}, "
            f"thresholds={r['thresholds']})"
        ),
    )
