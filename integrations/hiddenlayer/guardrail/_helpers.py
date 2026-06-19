"""Message extraction and payload mapping helpers for HiddenLayer interactions API."""

from __future__ import annotations

import copy
import json
from typing import Any, Optional


def _text_from_content(content: Any) -> str | None:
    """Flatten string or vision-style list-of-parts message content to text."""
    if isinstance(content, list):
        text_parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        joined = "\n".join(text_parts).strip()
        return joined or None
    if isinstance(content, str) and content.strip():
        return content
    return None


def tf_messages_to_hl(messages: list[Any]) -> tuple[list[dict[str, str]], list[int]]:
    """Convert TF chat messages to HL v1 shape, preserving source indices for redaction merge."""
    hl_messages: list[dict[str, str]] = []
    source_indices: list[int] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        text = _text_from_content(message.get("content"))
        if text is not None:
            hl_messages.append({"role": role, "content": text})
            source_indices.append(index)
    return hl_messages, source_indices


def tf_choices_to_hl_output(choices: list[Any]) -> tuple[list[dict[str, str]], list[int]]:
    """Convert responseBody.choices to HL output.messages with source choice indices."""
    hl_messages: list[dict[str, str]] = []
    source_indices: list[int] = []
    for index, choice in enumerate(choices):
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        text = _text_from_content(message.get("content"))
        if text is not None:
            role = str(message.get("role") or "assistant")
            hl_messages.append({"role": role, "content": text})
            source_indices.append(index)
    return hl_messages, source_indices


def has_scannable_input_messages(messages: list[Any]) -> bool:
    """True when the request carries any non-empty message content to analyze."""
    return any(
        isinstance(message, dict) and _text_from_content(message.get("content")) is not None
        for message in messages
    )


def has_scannable_output(choices: list[Any]) -> bool:
    """True when the response carries any non-empty assistant completion content."""
    return bool(tf_choices_to_hl_output(choices)[0])


def bodies_differ(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Stable deep comparison for deciding whether a mutate rail transformed content."""
    return json.dumps(left, sort_keys=True, default=str) != json.dumps(right, sort_keys=True, default=str)


def clone_body(body: dict[str, Any]) -> dict[str, Any]:
    """Return an isolated copy for mutate-rail result payloads."""
    return copy.deepcopy(body)


def _set_message_content(message: dict[str, Any], new_text: str) -> None:
    content = message.get("content")
    if isinstance(content, list):
        updated_parts = False
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                part["text"] = new_text
                updated_parts = True
                break
        if not updated_parts:
            message["content"] = new_text
    else:
        message["content"] = new_text


def apply_hl_input_to_request_body(
    request_body: dict[str, Any],
    modified_input: dict[str, Any],
    source_indices: list[int],
) -> dict[str, Any]:
    """Merge HiddenLayer modified_data.input back into the TrueFoundry requestBody."""
    updated = copy.deepcopy(request_body)
    hl_messages = modified_input.get("messages") or []
    tf_messages = updated.get("messages") or []
    if not isinstance(tf_messages, list) or not isinstance(hl_messages, list):
        return updated

    for hl_index, hl_message in enumerate(hl_messages):
        if hl_index >= len(source_indices) or not isinstance(hl_message, dict):
            continue
        tf_index = source_indices[hl_index]
        if tf_index >= len(tf_messages):
            continue
        tf_message = tf_messages[tf_index]
        if not isinstance(tf_message, dict):
            continue
        hl_content = hl_message.get("content")
        if isinstance(hl_content, str):
            _set_message_content(tf_message, hl_content)
    return updated


def apply_hl_output_to_response_body(
    response_body: dict[str, Any],
    modified_output: dict[str, Any],
    source_indices: list[int],
) -> dict[str, Any]:
    """Merge HiddenLayer modified_data.output back into the TrueFoundry responseBody."""
    updated = copy.deepcopy(response_body)
    hl_messages = modified_output.get("messages") or []
    choices = updated.get("choices") or []
    if not isinstance(choices, list) or not isinstance(hl_messages, list):
        return updated

    for hl_index, hl_message in enumerate(hl_messages):
        if hl_index >= len(source_indices) or not isinstance(hl_message, dict):
            continue
        choice_index = source_indices[hl_index]
        if choice_index >= len(choices):
            continue
        choice = choices[choice_index]
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        hl_content = hl_message.get("content")
        if isinstance(hl_content, str):
            _set_message_content(message, hl_content)
    return updated


def resolve_requester_id(config: Optional[dict[str, Any]], context: Any) -> str:
    """Resolve HiddenLayer metadata.requester_id from config or gateway context."""
    if config:
        for key in ("requesterId", "requester_id", "userId", "user_id"):
            value = config.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    user = getattr(context, "user", None) or {}
    if isinstance(user, dict):
        for key in ("subjectId", "subjectSlug", "subject_id"):
            value = user.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    metadata = getattr(context, "metadata", None) or {}
    if isinstance(metadata, dict):
        for key in ("request_id", "session_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return "truefoundry-user"


def resolve_session_id(config: Optional[dict[str, Any]], context: Any) -> Optional[str]:
    if config:
        for key in ("sessionId", "session_id", "externalSessionId", "external_session_id"):
            value = config.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    metadata = getattr(context, "metadata", None) or {}
    if isinstance(metadata, dict):
        for key in ("request_id", "session_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None
