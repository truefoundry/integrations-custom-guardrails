"""Payload translation between the TrueFoundry custom-guardrail contract and TrustGuard."""

from __future__ import annotations

import copy
from typing import Any, Optional

from entities import RequestContext

# Roles TrustGuard should score on the input leg. System/developer prompts are
# excluded: TrueFoundry docs state the gateway strips system prompts before any
# guardrail sees them, and TrustGuard leaves system instructions unscored anyway.
_SKIP_ROLES = {"system", "developer"}


def input_messages(request_body: dict) -> list[tuple[int, dict]]:
    """(index_in_requestBody, message) for every non-system message."""
    out: list[tuple[int, dict]] = []
    for i, m in enumerate(request_body.get("messages") or []):
        if isinstance(m, dict) and m.get("role") not in _SKIP_ROLES:
            out.append((i, m))
    return out


def output_messages(response_body: dict) -> list[tuple[int, dict]]:
    """(choice_index, assistant message) for each choice that carries a message."""
    out: list[tuple[int, dict]] = []
    for i, c in enumerate(response_body.get("choices") or []):
        msg = (c or {}).get("message")
        if isinstance(msg, dict) and (msg.get("content") or msg.get("tool_calls")):
            out.append((i, {"role": "assistant", **{k: v for k, v in msg.items() if k != "role"}}))
    return out


def correlation(context: RequestContext, request_body: dict) -> dict[str, Any]:
    """consumer_id / session_id / attributes for TrustGuard, from gateway context.

    Only documented TrustGuard attribute keys are emitted:
    consumer.{name,tag,type}, model.{name,provider}, source.application, user.{id,email}.
    """
    user = context.user or {}
    meta = context.metadata or {}
    consumer_id = user.get("subjectSlug") or user.get("subjectId")
    # ASSUMPTION: TrueFoundry forwards request metadata (X-TFY-METADATA) into
    # context.metadata; `session_id` is the key used in TrueFoundry's own examples.
    session_id = meta.get("session_id") or meta.get("sessionId")

    attributes: dict[str, Any] = {"source": {"application": "truefoundry-ai-gateway"}}
    model = request_body.get("model")
    if isinstance(model, str) and model:
        name = model
        provider = model.split("/", 1)[0] if "/" in model else None
        attributes["model"] = {"name": name, **({"provider": provider} if provider else {})}
    if user.get("subjectType"):
        attributes["consumer"] = {"type": str(user["subjectType"])}
    person = {}
    if user.get("subjectId"):
        person["id"] = str(user["subjectId"])
    slug = user.get("subjectSlug")
    if isinstance(slug, str) and "@" in slug:
        person["email"] = slug
    if person:
        attributes["user"] = person

    return {
        "consumer_id": str(consumer_id) if consumer_id else None,
        "session_id": str(session_id) if session_id else None,
        "attributes": attributes,
    }


def apply_messages(
    body: dict, container_key: str, positions: list[int], new_messages: Any, is_output: bool
) -> Optional[dict]:
    """Return a deep copy of `body` with TrustGuard's rewritten messages written back.

    Returns None when the transformed payload cannot be mapped 1:1 (caller fails closed).
    """
    if not isinstance(new_messages, list) or len(new_messages) != len(positions):
        return None
    out = copy.deepcopy(body)
    for pos, new in zip(positions, new_messages):
        if not isinstance(new, dict):
            return None
        if is_output:
            target = out["choices"][pos]["message"]
        else:
            target = out[container_key][pos]
        if "content" in new:
            target["content"] = new["content"]
        if "tool_calls" in new and isinstance(new["tool_calls"], list):
            target["tool_calls"] = new["tool_calls"]
    return out
