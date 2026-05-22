"""Output rail: NeMo Guardrails self_check_output flow.

Returns 2xx + {verdict: false, message: ...} on block per the TFY AI Gateway
custom guardrail contract.
"""

from entities import OutputGuardrailRequest, ValidateGuardrailResponse
from guardrail._nemo_runner import runner


async def self_check_output(request: OutputGuardrailRequest) -> ValidateGuardrailResponse:
    assistant_msg = _first_assistant_message(request.responseBody.get("choices") or [])
    if assistant_msg is None:
        return ValidateGuardrailResponse(verdict=True)
    last_user = _last_user_message(request.requestBody.get("messages") or []) or ""
    verdict = await runner.check_output(last_user, assistant_msg)
    if verdict.decision == "allow":
        return ValidateGuardrailResponse(verdict=True)
    return ValidateGuardrailResponse(
        verdict=False,
        message=verdict.refusal or "Blocked by NeMo self_check_output.",
    )


def _last_user_message(messages: list[dict]) -> str | None:
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


def _first_assistant_message(choices: list[dict]) -> str | None:
    for c in choices:
        msg = c.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str) and content:
            return content
    return None
