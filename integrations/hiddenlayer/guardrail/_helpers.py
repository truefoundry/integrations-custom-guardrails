"""Payload mapping helpers for HiddenLayer Detection v2 API."""

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


def tf_message_to_v2(message: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a TF/OpenAI chat message to v2 interaction canonical form."""
    text = _text_from_content(message.get("content"))
    if text is None:
        return None
    return {
        "role": str(message.get("role") or "user"),
        "content": [{"type": "text", "text": text}],
    }


def tf_messages_to_v2(messages: list[Any]) -> list[dict[str, Any]]:
    """Convert TF requestBody.messages to v2 interaction.messages."""
    v2_messages: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        converted = tf_message_to_v2(message)
        if converted is not None:
            v2_messages.append(converted)
    return v2_messages


def tf_choices_to_v2_messages(choices: list[Any]) -> list[dict[str, Any]]:
    """Convert responseBody.choices to v2 assistant interaction messages."""
    v2_messages: list[dict[str, Any]] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        if not isinstance(message, dict):
            continue
        converted = tf_message_to_v2({**message, "role": message.get("role") or "assistant"})
        if converted is not None:
            v2_messages.append(converted)
    return v2_messages


def interaction_messages_to_texts(messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Flatten v2 interaction messages to (role, text) pairs for comparison."""
    texts: list[tuple[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        parts = message.get("content") or []
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text")
                    if isinstance(text, str):
                        texts.append((role, text))
        elif isinstance(parts, str):
            texts.append((role, parts))
    return texts


def has_scannable_input_messages(messages: list[Any]) -> bool:
    """True when the request carries any non-empty message content to analyze."""
    return any(
        isinstance(message, dict) and _text_from_content(message.get("content")) is not None
        for message in messages
    )


def has_scannable_output(choices: list[Any]) -> bool:
    """True when the response carries any non-empty assistant completion content."""
    return bool(tf_choices_to_v2_messages(choices))


def bodies_differ(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Stable deep comparison for deciding whether a mutate rail transformed content."""
    return json.dumps(left, sort_keys=True, default=str) != json.dumps(right, sort_keys=True, default=str)


def clone_body(body: dict[str, Any]) -> dict[str, Any]:
    """Return an isolated copy for mutate-rail result payloads."""
    return copy.deepcopy(body)


def build_v2_metadata(
    request_body: dict[str, Any],
    config: Optional[dict[str, Any]],
    context: Any,
    *,
    provider: str,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "model": str(request_body.get("model") or "unknown"),
        "requester_id": resolve_requester_id(config, context),
        "provider": provider,
    }
    session_id = resolve_session_id(config, context)
    if session_id:
        metadata["external_session_id"] = session_id
    return metadata


def build_interaction_evaluations_payload(
    *,
    request_body: dict[str, Any],
    config: Optional[dict[str, Any]],
    context: Any,
    provider: str,
    response_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build POST /detection/v2/interaction-evaluations body."""
    messages = tf_messages_to_v2(request_body.get("messages") or [])
    if response_body is not None:
        messages.extend(tf_choices_to_v2_messages(response_body.get("choices") or []))

    return {
        "metadata": build_v2_metadata(request_body, config, context, provider=provider),
        "interaction": {"messages": messages},
    }


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
