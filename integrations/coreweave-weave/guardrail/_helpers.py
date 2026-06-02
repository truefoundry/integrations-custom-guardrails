"""Shared message-extraction and mutation helpers (per integration; not cross-integration)."""

from __future__ import annotations

import copy
from typing import Any


def last_user_text(messages: list[dict]) -> str | None:
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


def first_assistant_text(choices: list[dict]) -> str | None:
    """Return the first non-empty assistant message content from `responseBody.choices`."""
    for c in choices:
        msg = c.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str) and content:
            return content
    return None


def replace_last_user_content(request_body: dict[str, Any], new_content: str) -> dict[str, Any]:
    """Return a deep copy of `request_body` with the last user message's `content` replaced.

    For vision-style list-of-parts content the entire content is collapsed to a single
    string part. If no user message is present the body is returned unchanged.
    """
    body = copy.deepcopy(request_body)
    messages = body.get("messages") or []
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            messages[i]["content"] = new_content
            return body
    return body


def replace_first_assistant_content(response_body: dict[str, Any], new_content: str) -> dict[str, Any]:
    """Return a deep copy of `response_body` with the first assistant choice's `content` replaced.

    If no choices are present, returns the body unchanged.
    """
    body = copy.deepcopy(response_body)
    choices = body.get("choices") or []
    if not choices:
        return body
    msg = choices[0].get("message") or {}
    msg["content"] = new_content
    choices[0]["message"] = msg
    return body
