"""MCP tool pre-invoke rail: ask Bigeye whether the tool call may touch the data it names.

Operation: Validate. The gateway awaits this verdict before running the tool,
so ``verdict: false`` means the tool never executes. Bigeye failures raise
BigeyeCallError (502/504) and misconfiguration raises ConfigError (500); the
guardrail's enforcing strategy decides what those mean.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import Depends

from guardrail._auth import bigeye_api_key
from guardrail._bigeye_client import post_access_decision
from guardrail._decision import (
    block_message,
    blocking_decision,
    build_request_body,
    parse_response,
    summary,
    unchecked_summary,
)
from guardrail._extract import extract_targets
from guardrail._payload_log import log_payload, payload_logging_enabled
from guardrail._settings import ConfigError, parse_settings
from mcp_entities import BigeyeValidateResponse, McpPreInvokeRequest

log = logging.getLogger("bigeye-guardrails-tfy")

NOTHING_TO_CHECK = "no SQL, tables or columns in arguments"


async def mcp_pre_invoke(
    payload: McpPreInvokeRequest,
    api_key: str = Depends(bigeye_api_key),
) -> BigeyeValidateResponse:
    if payload_logging_enabled():
        log_payload(payload.model_dump(exclude_unset=True))

    settings = parse_settings(payload.config)
    targets = extract_targets(payload.requestBody)
    if targets.empty:
        log.info("bigeye skipped: %s", NOTHING_TO_CHECK)
        return BigeyeValidateResponse(verdict=True, bigeye=unchecked_summary(NOTHING_TO_CHECK))
    if targets.sql and not settings.source_name:
        log.error("tool arguments contain SQL but the guardrail config has no source_name")
        raise ConfigError("source_name is required to check SQL")

    span_id = str(uuid.uuid4())
    body = build_request_body(targets, settings, payload.context.user, span_id)
    data, latency_ms = await post_access_decision(settings, body, api_key)
    parsed = parse_response(data)
    blocked_by = blocking_decision(parsed, settings.block_on)

    log.info(
        "bigeye decision=%s verdict=%s assets=%d unresolved=%d latency_ms=%d span=%s",
        parsed.overall,
        blocked_by is None,
        len(parsed.assets),
        parsed.unresolved_count,
        latency_ms,
        span_id,
    )
    return BigeyeValidateResponse(
        verdict=blocked_by is None,
        message=block_message(parsed, blocked_by) if blocked_by else None,
        bigeye=summary(parsed, span_id, latency_ms),
    )
