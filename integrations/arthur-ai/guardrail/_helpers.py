"""Message extraction helpers for Arthur validate payloads."""

from __future__ import annotations

from typing import Any


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


def last_user_text(messages: list[dict]) -> str | None:
    """Return the most recent user message content."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        text = _text_from_content(message.get("content"))
        if text is not None:
            return text
    return None


def first_assistant_text(choices: list[dict]) -> str | None:
    """Return the first assistant completion content."""
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        text = _text_from_content(message.get("content"))
        if text is not None:
            return text
    return None


def system_context_text(messages: list[dict]) -> str | None:
    """Join system-role messages for grounding / hallucination checks."""
    parts: list[str] = []
    for message in messages:
        if message.get("role") != "system":
            continue
        text = _text_from_content(message.get("content"))
        if text is not None:
            parts.append(text)
    return "\n\n".join(parts) if parts else None
