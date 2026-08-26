"""Shared message-extraction helpers (per integration; not cross-integration).

Onyx /simple wants extracted text (``user_prompt`` / ``response``), so these
helpers both short-circuit empty traffic and supply the payload for evaluate().
"""

from __future__ import annotations


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
