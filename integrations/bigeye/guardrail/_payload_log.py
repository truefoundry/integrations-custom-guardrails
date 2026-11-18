"""Opt-in logging of incoming gateway payloads (``LOG_PAYLOADS=true``).

Identity values (``context.user`` except ``subjectType``, ``context.claims``,
``context.metadata`` and ``config.headers``) become ``<redacted:type>`` with
keys kept, so the log shows which fields arrive without their contents. Long
strings are truncated. Request headers are never logged.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("bigeye-guardrails-tfy")

MAX_LOGGED_STRING_CHARS = 500
KEPT_USER_KEYS = frozenset({"subjectType"})


def payload_logging_enabled() -> bool:
    return os.environ.get("LOG_PAYLOADS", "").strip().lower() in ("1", "true", "yes")


def _redact(value: object) -> object:
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if value is None:
        return None
    return f"<redacted:{type(value).__name__}>"


def _truncate(value: object) -> object:
    if isinstance(value, dict):
        return {k: _truncate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate(v) for v in value]
    if isinstance(value, str) and len(value) > MAX_LOGGED_STRING_CHARS:
        return f"{value[:MAX_LOGGED_STRING_CHARS]}...<truncated {len(value)} chars>"
    return value


def redact_payload(payload: dict) -> dict:
    redacted = {k: _truncate(v) for k, v in payload.items()}
    context = redacted.get("context")
    if isinstance(context, dict):
        user = context.get("user")
        if isinstance(user, dict):
            context["user"] = {k: (v if k in KEPT_USER_KEYS else _redact(v)) for k, v in user.items()}
        for key in ("claims", "metadata"):
            if key in context:
                context[key] = _redact(context[key])
    config = redacted.get("config")
    if isinstance(config, dict) and "headers" in config:
        config["headers"] = _redact(config["headers"])
    return redacted


def log_payload(payload: dict) -> None:
    log.info("incoming payload: %s", json.dumps(redact_payload(payload), default=str))
