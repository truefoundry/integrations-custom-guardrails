"""NeuralTrust TrustGuard rails for the TrueFoundry AI Gateway custom-guardrail contract.

TrueFoundry contract (https://www.truefoundry.com/docs/ai-gateway/custom-guardrails,
https://github.com/truefoundry/integrations-custom-guardrails/blob/main/docs/gateway-contract.md):
  200 {"verdict": true}                                  -> allow
  200 {"verdict": false, "message": "..."}               -> block (policy)
  200 {"verdict": true, "transformed": true, "result": {full body}}  -> mutate (Mutate configs only)
  5xx                                                    -> guardrail error (Enforcing Strategy decides)

TrustGuard verdict mapping (status from POST /v1/evaluate):
  allow, skip, report -> verdict true   (report/skip are logged with trace_id)
  block               -> verdict false
  ask                 -> verdict false by default (gateway has no approval flow; TRUSTGUARD_ASK_ACTION)
  transform           -> Mutate route: apply transformed_payload, fail closed if it cannot be mapped
                         Validate route: verdict true by default (TRUSTGUARD_TRANSFORM_ON_VALIDATE)

Fail mode: if TrustGuard times out (NEURALTRUST_TIMEOUT_SECONDS, default 3.5 s), is unreachable,
or answers non-200, the wrapper returns HTTP 502. It never converts an error into an allow.
The TrueFoundry Enforcing Strategy then decides:
  Enforce                      -> request blocked   (fail closed)
  Enforce But Ignore On Error  -> request continues (fail open)
  Audit                        -> request continues (log only)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Union

from fastapi import HTTPException

from entities import (
    InputGuardrailRequest,
    MutateGuardrailResponse,
    OutputGuardrailRequest,
    ValidateGuardrailResponse,
)
from guardrail import _trustguard_client as tg
from guardrail._helpers import apply_messages, correlation, input_messages, output_messages

log = logging.getLogger("neuraltrust-guardrails-tfy")

Resp = Union[ValidateGuardrailResponse, MutateGuardrailResponse]


def _opt(config: dict | None, key: str, env: str, default: str) -> str:
    """Per-config override (dashboard Config JSON) > env var > default."""
    if config and isinstance(config.get(key), str):
        return config[key].strip().lower()
    return os.environ.get(env, default).strip().lower()


def _block_message(result: dict, direction: str) -> str:
    signals = sorted(
        {
            str((f.get("signal") or {}).get("type"))
            for f in result.get("findings") or []
            if isinstance(f, dict) and (f.get("signal") or {}).get("type")
        }
    )
    # Findings evidence is NOT echoed to the caller (may contain the sensitive data itself).
    sig = f" signals={','.join(signals)}" if signals else ""
    return (
        f"NeuralTrust TrustGuard ({direction}): {result.get('status')}{sig}"
        f" trace_id={result.get('trace_id')}"
    )[:500]


def _decide_non_transform(result: dict, direction: str, config: dict | None) -> ValidateGuardrailResponse | None:
    """Handle every status except `transform`. Returns None for `transform`."""
    status = str(result.get("status") or "").lower()
    if status in ("allow", "skip", "report"):
        if status != "allow":
            log.info("trustguard %s status=%s trace_id=%s", direction, status, result.get("trace_id"))
        return ValidateGuardrailResponse(verdict=True)
    if status == "block":
        return ValidateGuardrailResponse(verdict=False, message=_block_message(result, direction))
    if status == "ask":
        if _opt(config, "ask_action", "TRUSTGUARD_ASK_ACTION", "block") == "allow":
            log.warning("trustguard ask treated as allow trace_id=%s", result.get("trace_id"))
            return ValidateGuardrailResponse(verdict=True)
        return ValidateGuardrailResponse(verdict=False, message=_block_message(result, direction))
    if status == "transform":
        return None
    # Unknown status: fail closed rather than silently allow.
    raise HTTPException(status_code=502, detail=f"unknown TrustGuard status: {status!r}")


async def _call(config: dict | None, **kwargs: Any) -> dict:
    try:
        return await tg.evaluate(timeout=tg.resolve_timeout(config), **kwargs)
    except tg.TrustGuardUnavailable as exc:
        log.error("trustguard unavailable: %s", exc)
        # 5xx -> TrueFoundry Enforcing Strategy decides (Enforce = block, Ignore On Error = pass).
        raise HTTPException(status_code=502, detail={"error": "TrustGuard unavailable", "detail": str(exc)})


# --------------------------------------------------------------------------- LLM input


async def _llm_input(request: InputGuardrailRequest, mutate: bool) -> Resp:
    body = request.requestBody
    picked = input_messages(body)
    if not picked:
        return ValidateGuardrailResponse(verdict=True)
    positions = [i for i, _ in picked]
    msgs = [m for _, m in picked]
    result = await _call(
        request.config,
        payload={"messages": msgs},
        direction="input",
        protocol="llm",
        **correlation(request.context, body),
    )
    decided = _decide_non_transform(result, "input", request.config)
    if decided is not None:
        if mutate and decided.verdict:
            return MutateGuardrailResponse(verdict=True, transformed=False, result=body)
        return decided
    # status == transform
    if not mutate:
        if _opt(request.config, "transform_on_validate", "TRUSTGUARD_TRANSFORM_ON_VALIDATE", "allow") == "block":
            return ValidateGuardrailResponse(verdict=False, message=_block_message(result, "input"))
        return ValidateGuardrailResponse(verdict=True)
    new_msgs = (result.get("transformed_payload") or {}).get("messages")
    new_body = apply_messages(body, "messages", positions, new_msgs, is_output=False)
    if new_body is None:
        return ValidateGuardrailResponse(
            verdict=False, message="NeuralTrust TrustGuard (input): transform could not be applied"
        )
    return MutateGuardrailResponse(verdict=True, transformed=True, result=new_body)


async def validate_input(request: InputGuardrailRequest) -> Resp:
    return await _llm_input(request, mutate=False)


async def redact_input(request: InputGuardrailRequest) -> Resp:
    return await _llm_input(request, mutate=True)


# --------------------------------------------------------------------------- LLM output


async def _llm_output(request: OutputGuardrailRequest, mutate: bool) -> Resp:
    body = request.responseBody
    picked = output_messages(body)
    if not picked:
        return ValidateGuardrailResponse(verdict=True)
    positions = [i for i, _ in picked]
    msgs = [m for _, m in picked]
    result = await _call(
        request.config,
        payload={"messages": msgs},  # completion as assistant messages, per TrustGuard docs
        direction="output",
        protocol="llm",
        **correlation(request.context, request.requestBody),
    )
    decided = _decide_non_transform(result, "output", request.config)
    if decided is not None:
        if mutate and decided.verdict:
            return MutateGuardrailResponse(verdict=True, transformed=False, result=body)
        return decided
    if not mutate:
        if _opt(request.config, "transform_on_validate", "TRUSTGUARD_TRANSFORM_ON_VALIDATE", "allow") == "block":
            return ValidateGuardrailResponse(verdict=False, message=_block_message(result, "output"))
        return ValidateGuardrailResponse(verdict=True)
    new_msgs = (result.get("transformed_payload") or {}).get("messages")
    new_body = apply_messages(body, "choices", positions, new_msgs, is_output=True)
    if new_body is None:
        return ValidateGuardrailResponse(
            verdict=False, message="NeuralTrust TrustGuard (output): transform could not be applied"
        )
    return MutateGuardrailResponse(verdict=True, transformed=True, result=new_body)


async def validate_output(request: OutputGuardrailRequest) -> Resp:
    return await _llm_output(request, mutate=False)


async def redact_output(request: OutputGuardrailRequest) -> Resp:
    return await _llm_output(request, mutate=True)
