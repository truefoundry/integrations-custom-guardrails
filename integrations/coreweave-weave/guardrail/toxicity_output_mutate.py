"""Output rail (Operation: Mutate): replace the assistant response on toxicity detect.

Pattern: same scorer call as `toxicity_output.py`, but instead of returning a
block verdict we replace the assistant message's content with a fixed canned
refusal and return a `MutateGuardrailResponse`. The client then sees the canned
text instead of the toxic generation.

Celadon is a scorer (not a rewriter), so the canned refusal is a fixed string
rather than a sanitized rewrite of the model's output.
"""

from __future__ import annotations

from entities import MutateGuardrailResponse, OutputGuardrailRequest
from guardrail._helpers import first_assistant_text, replace_first_assistant_content
from guardrail._weave_runner import evaluate

OUTPUT_MASK = "I can't help with that."


def toxicity_output_mutate(request: OutputGuardrailRequest) -> MutateGuardrailResponse:
    assistant_msg = first_assistant_text(request.responseBody.get("choices") or [])
    if assistant_msg is None:
        return MutateGuardrailResponse(verdict=True, transformed=False, result=request.responseBody)

    r = evaluate(assistant_msg, request.config)
    if r["passed"]:
        return MutateGuardrailResponse(verdict=True, transformed=False, result=request.responseBody)

    masked = replace_first_assistant_content(request.responseBody, OUTPUT_MASK)
    return MutateGuardrailResponse(verdict=True, transformed=True, result=masked)
