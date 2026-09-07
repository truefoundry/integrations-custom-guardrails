"""RepelloAI Argus redact rails (dashboard `Operation: Mutate`).

Argus returns `masked_result`: a full replacement for exactly the text that was
submitted. The wrapper scans one content unit per call, so each unit's masked
string is written straight back to the field it came from and no offsets are
ever reconstructed.

Three safety rules govern the result, in order:

1. A non-null `masked_result` is never permission to allow. The verdict comes
   from `policies_violated[].action_taken`, since a block can come from a
   non-redacting policy while a redacting one did the masking.
2. Blocked content is never forwarded redacted. Any blocking policy denies and
   returns the original body.
3. Content that was flagged but could not be redacted is never forwarded. A
   redacting policy that fired with no mask means masking was attempted and
   failed, which is indistinguishable from having nothing to mask. A unit longer
   than the scan cap falls here too: Argus masked only the truncated prefix, so
   writing that mask back would drop the unscanned tail.

Neither `masked_result` nor scanned text is ever logged.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

from entities import (
    InputGuardrailRequest,
    MutateGuardrailResponse,
    OutputGuardrailRequest,
)
from guardrail._helpers import ASSISTANT_TEXT, TOOL_CALL, TOOL_RESULT, USER_MESSAGE
from guardrail.argus import ScanOutcome, UnitResult, max_text_chars, scan_request

logger = logging.getLogger(__name__)


def _clone_body(body: dict[str, Any]) -> dict[str, Any]:
    """Return an isolated copy for the mutate result payload."""
    return copy.deepcopy(body)


def _bodies_differ(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Stable deep comparison deciding whether the rail transformed content.

    Comparing the bodies is self-checking: `transformed` cannot disagree with
    what was actually written.
    """
    return json.dumps(left, sort_keys=True, default=str) != json.dumps(
        right, sort_keys=True, default=str
    )


def _deny(body: dict[str, Any]) -> MutateGuardrailResponse:
    """Deny with the original body.

    `MutateGuardrailResponse` has no `message` field and the gateway's mutate
    branch reads only `verdict` and `transformed`, so the reason is logged
    rather than returned.
    """
    return MutateGuardrailResponse(verdict=False, transformed=False, result=_clone_body(body))


def _apply_to_message(message: dict[str, Any], masked: str) -> bool:
    """Write a mask back to a message's `content`. False if the shape is unwritable.

    List-shaped content is refused: `flatten_content` joins the text parts with
    newlines to scan them, which is not invertible, and writing the joined mask
    back as a bare string would delete any image parts.
    """
    if not isinstance(message, dict) or isinstance(message.get("content"), list):
        return False
    message["content"] = masked
    return True


def _apply_to_tool_call(tool_call: dict[str, Any], masked: str) -> bool:
    """Write a mask back to a tool call's `arguments`, preserving its type.

    `arguments` arrives as either a JSON string (OpenAI) or an already-decoded
    dict/list (other providers). Writing a string into a field that held a dict
    would change the body's type downstream, so the parsed object is restored
    whenever the masked text still round-trips. When masking breaks the JSON the
    masked string is written anyway: redacted-but-restringified beats forwarding
    the unredacted value.
    """
    if not isinstance(tool_call, dict):
        return False
    function = tool_call.get("function")
    if not isinstance(function, dict):
        return False

    if isinstance(function.get("arguments"), (dict, list)):
        try:
            function["arguments"] = json.loads(masked)
        except (TypeError, ValueError):
            function["arguments"] = masked
    else:
        function["arguments"] = masked
    return True


def _locate(body: dict[str, Any], result: UnitResult) -> dict[str, Any] | None:
    """Resolve a unit back to the container holding its text.

    Uses the structural indices carried on `ContentUnit`; `ref` is a log string
    and is never parsed.
    """
    unit = result.unit

    if unit.kind in (USER_MESSAGE, TOOL_RESULT):
        messages = body.get("messages")
        if not isinstance(messages, list) or unit.index >= len(messages):
            return None
        message = messages[unit.index]
        return message if isinstance(message, dict) else None

    choices = body.get("choices")
    if not isinstance(choices, list) or unit.index >= len(choices):
        return None
    choice = choices[unit.index]
    if not isinstance(choice, dict):
        return None
    message = choice.get("message")
    if not isinstance(message, dict):
        return None

    if unit.kind == ASSISTANT_TEXT:
        return message

    tool_calls = message.get("tool_calls")
    if (
        unit.tool_call_index is None
        or not isinstance(tool_calls, list)
        or unit.tool_call_index >= len(tool_calls)
    ):
        return None
    tool_call = tool_calls[unit.tool_call_index]
    return tool_call if isinstance(tool_call, dict) else None


def _redact(
    outcome: ScanOutcome,
    body: dict[str, Any],
    direction: str,
) -> MutateGuardrailResponse:
    """Apply every unit's mask to a copy of the body, enforcing the safety rules."""
    if outcome.blocked:
        # Rule 2: a blocked body is never forwarded, redacted or otherwise.
        logger.info(
            "Argus blocked %s content on the redact rail: %s",
            direction,
            ", ".join(outcome.blocking_policies) or "policy violation",
        )
        return _deny(body)

    result_body = _clone_body(body)

    for result in outcome.results:
        if result.masked_result is None:
            # Rule 3: a redacting policy fired but produced no mask, so masking
            # was attempted and failed. Denying is the only safe reading.
            if result.redacting_fired:
                logger.warning(
                    "Argus flagged %s content at %s but returned no mask; denying. "
                    "Policies: %s",
                    direction,
                    result.unit.ref,
                    ", ".join(result.flagged_policies) or "unknown",
                )
                return _deny(body)
            continue

        if len(result.unit.text) > max_text_chars():
            # Argus masks exactly the text it was sent, which is truncated to the
            # scan cap. Writing that mask back would delete the unscanned tail.
            logger.warning(
                "Cannot redact %s in the %s body: it exceeds the %d-character scan "
                "cap, so the mask covers only the scanned prefix. Denying.",
                result.unit.ref,
                direction,
                max_text_chars(),
            )
            return _deny(body)

        container = _locate(result_body, result)
        if container is None:
            logger.warning(
                "Redaction target %s no longer resolves in the %s body; denying.",
                result.unit.ref,
                direction,
            )
            return _deny(body)

        if result.unit.kind == TOOL_CALL:
            written = _apply_to_tool_call(container, result.masked_result)
        else:
            written = _apply_to_message(container, result.masked_result)

        if not written:
            logger.warning(
                "Cannot redact %s in the %s body: list-shaped content is scanned "
                "as joined text and cannot be rewritten in place. Denying. "
                "Policies: %s",
                result.unit.ref,
                direction,
                ", ".join(result.flagged_policies) or "unknown",
            )
            return _deny(body)

    if outcome.flagged_policies:
        logger.info(
            "Argus flagged %s content without blocking: %s",
            direction,
            ", ".join(outcome.flagged_policies),
        )

    return MutateGuardrailResponse(
        verdict=True,
        transformed=_bodies_differ(body, result_body),
        result=result_body,
    )


async def redact_input(request: InputGuardrailRequest) -> MutateGuardrailResponse:
    """Input redact rail: mask the latest user message and any new tool results.

    Argus applies no redacting policy on the prompt endpoint today, so this rail
    enforces blocks and passes content through unchanged. It is written against
    the same contract as the output rail, so it begins redacting with no change
    here the day a redacting policy is enabled for prompts.
    """
    body = request.requestBody or {}
    outcome = await scan_request(request, "input")
    if not outcome.unit_count:
        logger.warning(
            "Input redact rail found no scannable content; forwarding unchanged"
        )
        return MutateGuardrailResponse(
            verdict=True, transformed=False, result=_clone_body(body)
        )
    return _redact(outcome, body, "input")


async def redact_output(request: OutputGuardrailRequest) -> MutateGuardrailResponse:
    """Output redact rail: mask every populated choice and every tool call's arguments."""
    body = request.responseBody or {}
    outcome = await scan_request(request, "output")
    if not outcome.unit_count:
        logger.warning(
            "Output redact rail found no scannable content; forwarding unchanged"
        )
        return MutateGuardrailResponse(
            verdict=True, transformed=False, result=_clone_body(body)
        )
    return _redact(outcome, body, "output")
