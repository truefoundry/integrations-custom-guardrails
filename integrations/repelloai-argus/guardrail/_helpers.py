"""Content-unit extraction for the RepelloAI Argus rails.

Every Argus call scans one standalone piece of text with no history, so a
gateway payload is split into independent units rather than concatenated into a
transcript. Only content new in this request is extracted; earlier turns were
scanned when they first passed through the gateway.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

USER_MESSAGE = "user_message"
TOOL_RESULT = "tool_result"
ASSISTANT_TEXT = "assistant_text"
TOOL_CALL = "tool_call"


@dataclass(frozen=True)
class ContentUnit:
    """One standalone piece of content to scan.

    `ref` is a human-readable locator (e.g. `choices[0].tool_calls[1]`) used in
    logs and Argus metadata. It never contains scanned text.

    `index` and `tool_call_index` locate the source field structurally so a
    redact rail can write a mask back to it. `ref` is never parsed for that.
    """

    text: str
    kind: str
    ref: str
    index: int
    tool_call_index: int | None = None


def flatten_content(content: Any) -> str | None:
    """Normalise an OpenAI `content` field to text.

    Handles plain strings and vision-style list-of-parts. Returns None when there
    is nothing textual to scan (e.g. an image-only message, or a message whose
    content is None because the model emitted tool calls instead).
    """
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        parts = [
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        return "\n".join(parts).strip() or None
    return None


def _tool_call_arguments(tool_call: Any) -> str | None:
    """Serialise a tool call's arguments to scannable text.

    OpenAI sends `arguments` as a JSON *string*, but several providers and SDKs
    hand back an already-decoded dict, so both are accepted.
    """
    if not isinstance(tool_call, dict):
        return None
    function = tool_call.get("function")
    if not isinstance(function, dict):
        return None
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        return arguments.strip() or None
    if isinstance(arguments, (dict, list)):
        try:
            return json.dumps(arguments, ensure_ascii=False)
        except (TypeError, ValueError):
            return None
    return None


def input_units(request_body: dict[str, Any]) -> list[ContentUnit]:
    """Extract the latest user message plus any tool results that follow it.

    Tool results carry retrieved content (RAG chunks, MCP responses) and are a
    common prompt-injection vector.
    """
    messages = request_body.get("messages")
    if not isinstance(messages, list):
        return []

    last_user_index = -1
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            last_user_index = index

    units: list[ContentUnit] = []

    if last_user_index >= 0:
        text = flatten_content(messages[last_user_index].get("content"))
        if text and text.strip():
            units.append(
                ContentUnit(
                    text=text,
                    kind=USER_MESSAGE,
                    ref=f"messages[{last_user_index}]",
                    index=last_user_index,
                )
            )

    # Tool results follow the user turn that triggered them; anything earlier
    # belongs to a previous turn.
    for index in range(last_user_index + 1, len(messages)):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        text = flatten_content(message.get("content"))
        if text and text.strip():
            units.append(
                ContentUnit(
                    text=text,
                    kind=TOOL_RESULT,
                    ref=f"messages[{index}]",
                    index=index,
                )
            )

    return units


def output_units(response_body: dict[str, Any]) -> list[ContentUnit]:
    """Extract every populated choice and every tool call's arguments.

    A tool-calling response has `content: None`, so reading only `content` would
    skip the responses that can exfiltrate data through tool arguments.
    """
    choices = response_body.get("choices")
    if not isinstance(choices, list):
        return []

    units: list[ContentUnit] = []
    for choice_index, choice in enumerate(choices):
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue

        text = flatten_content(message.get("content"))
        if text and text.strip():
            units.append(
                ContentUnit(
                    text=text,
                    kind=ASSISTANT_TEXT,
                    ref=f"choices[{choice_index}].message.content",
                    index=choice_index,
                )
            )

        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for call_index, tool_call in enumerate(tool_calls):
            arguments = _tool_call_arguments(tool_call)
            if not arguments:
                continue
            units.append(
                ContentUnit(
                    text=arguments,
                    kind=TOOL_CALL,
                    ref=f"choices[{choice_index}].tool_calls[{call_index}]",
                    index=choice_index,
                    tool_call_index=call_index,
                )
            )

    return units
