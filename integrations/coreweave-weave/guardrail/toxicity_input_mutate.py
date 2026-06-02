"""Input rail (Operation: Mutate): mask the user message on toxicity detect.

Pattern: same scorer call as `toxicity_input.py`, but instead of returning a
block verdict we replace the user message's content with a fixed placeholder
and return a `MutateGuardrailResponse`. The gateway then forwards the masked
requestBody to the model, which sees a benign placeholder and responds in kind.

Celadon is a scorer (not a rewriter), so the placeholder is a fixed string
rather than a sanitized rewrite of the original. For semantically-preserving
rewrites use a vendor that ships a rewriter (e.g. `integrations/lasso-security/`
classifix).
"""

from __future__ import annotations

from entities import InputGuardrailRequest, MutateGuardrailResponse
from guardrail._helpers import last_user_text, replace_last_user_content
from guardrail._weave_runner import evaluate

INPUT_MASK = "[message removed by safety filter]"


def toxicity_input_mutate(request: InputGuardrailRequest) -> MutateGuardrailResponse:
    user_msg = last_user_text(request.requestBody.get("messages") or [])
    if user_msg is None:
        return MutateGuardrailResponse(verdict=True, transformed=False, result=request.requestBody)

    r = evaluate(user_msg, request.config)
    if r["passed"]:
        return MutateGuardrailResponse(verdict=True, transformed=False, result=request.requestBody)

    masked = replace_last_user_content(request.requestBody, INPUT_MASK)
    return MutateGuardrailResponse(verdict=True, transformed=True, result=masked)
