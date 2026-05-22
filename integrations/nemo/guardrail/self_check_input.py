"""Input rail: NeMo Guardrails self_check_input flow.

Returns 2xx + {verdict: false, message: ...} on block per the TFY AI Gateway
custom guardrail contract. Block reason is the rail's refusal text (the
content NeMo substituted for the input). Allow returns {verdict: true}.
"""

from entities import InputGuardrailRequest, ValidateGuardrailResponse
from guardrail._nemo_runner import runner


async def self_check_input(request: InputGuardrailRequest) -> ValidateGuardrailResponse:
    user_msg = _last_user_message(request.requestBody.get("messages") or [])
    if user_msg is None:
        return ValidateGuardrailResponse(verdict=True)
    verdict = await runner.check_input(user_msg)
    if verdict.decision == "allow":
        return ValidateGuardrailResponse(verdict=True)
    return ValidateGuardrailResponse(
        verdict=False,
        message=verdict.refusal or "Blocked by NeMo self_check_input.",
    )


def _last_user_message(messages: list[dict]) -> str | None:
    """Return the most recent user message content. Flatten vision-style list-of-parts to text."""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, list):
            text_parts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            return ("\n".join(text_parts).strip()) or None
        if isinstance(content, str):
            return content
    return None
