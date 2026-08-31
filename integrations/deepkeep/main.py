"""
DeepKeep <-> tfy-llm-gateway custom guardrail wrapper.

Translates the gateway's InputGuardrailRequest / OutputGuardrailRequest
into DeepKeep's v3 OpenAI-compatible moderation endpoints, then maps
DeepKeep's ApplyGuardrailResponse onto the gateway Mutate contract
(https://www.truefoundry.com/docs/ai-gateway/custom-guardrails):

  HTTP 2xx + {verdict, transformed, result}  — policy completed
    verdict=true,  transformed=false  → pass-through
    verdict=true,  transformed=true   → mutate (PII redact/modify)
    verdict=false                     → deny (jailbreak / toxic / secrets block)
  HTTP 5xx                               — DeepKeep / wrapper infrastructure failure

Never return HTTP 4xx for a policy deny: the gateway treats 4xx as a runtime
error (Enforce blocks it as "guardrail API failed"; ignore-on-error lets it
through unredacted).

DeepKeep endpoints:
  POST /api/v3/openai/moderations/pre   (input firewall)
  POST /api/v3/openai/moderations/post  (output firewall)

Auth to DeepKeep: X-API-Key header.
Auth to this wrapper: Authorization: Bearer $WRAPPER_API_KEY (required when
deployed with Port expose=True; optional for local development only).
"""

import asyncio
import copy
import logging
import os
from typing import Any, Literal

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("deepkeep-guardrail-wrapper")

# ---------------------------------------------------------------------------
# Config — set these via env vars when deploying the wrapper
# ---------------------------------------------------------------------------
DEEPKEEP_BASE_URL = (
    os.environ.get("DEEPKEEP_BASE_URL") or "https://api.poc2.aws.deepkeep.ai"
)
DEEPKEEP_API_KEY = os.environ["DEEPKEEP_API_KEY"]  # required
DEEPKEEP_INPUT_FIREWALL_ID = os.environ["DEEPKEEP_INPUT_FIREWALL_ID"]
DEEPKEEP_OUTPUT_FIREWALL_ID = (
    os.environ.get("DEEPKEEP_OUTPUT_FIREWALL_ID") or DEEPKEEP_INPUT_FIREWALL_ID
)

# Shared bearer token for gateway → wrapper auth. Configure the same value in
# the TrueFoundry dashboard under Custom Bearer Auth. When unset, auth is
# disabled (local development only) — deploy.py must inject WRAPPER_API_KEY
# for any publicly exposed deployment.
WRAPPER_API_KEY = os.environ.get("WRAPPER_API_KEY", "").strip()


def require_bearer(request: Request) -> None:
    """Bearer-auth dependency. No key configured -> auth disabled (local dev only)."""
    if not WRAPPER_API_KEY:
        return
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if header.split(" ", 1)[1].strip() != WRAPPER_API_KEY:
        raise HTTPException(status_code=401, detail="invalid bearer token")


app = FastAPI(title="DeepKeep Guardrail Wrapper")

DEEPKEEP_TIMEOUT_SECONDS = float(os.environ.get("DEEPKEEP_TIMEOUT_SECONDS", "10"))

# A hibernating DeepKeep firewall answers 503 with a "warming up" / "waking from
# hibernate" detail while it spins up. Retry briefly instead of failing the request.
DEEPKEEP_WARMUP_RETRIES = int(os.environ.get("DEEPKEEP_WARMUP_RETRIES", "3"))
DEEPKEEP_WARMUP_BACKOFF_SECONDS = float(
    os.environ.get("DEEPKEEP_WARMUP_BACKOFF_SECONDS", "2")
)
WARMUP_MARKERS = ("warming up", "waking from hibernate", "hibernat")

# If DeepKeep is unreachable/erroring, fail open (pass traffic through) by default.
# Set to "false" to fail closed (block traffic) instead.
DEEPKEEP_FAIL_OPEN = os.environ.get("DEEPKEEP_FAIL_OPEN", "true").lower() != "false"

client = httpx.AsyncClient(
    base_url=DEEPKEEP_BASE_URL,
    headers={"X-API-Key": DEEPKEEP_API_KEY, "Content-Type": "application/json"},
    timeout=DEEPKEEP_TIMEOUT_SECONDS,
)

# DeepKeep: first-listed fired guardrail in verbosity wins (firewall config order).
# Current Pre order: Credentials Leakage: Secret Key [block]
#   → PII Detector [replace] → Adversarial Prompt Defense [block] → Toxic Language [block]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _extract_last_text(messages: list[dict[str, Any]], role: str) -> tuple[str, int]:
    """Return (text, index) of the last message with the given role."""
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].get("role") == role:
            content = messages[idx].get("content", "")
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            return content, idx
    return "", -1


class DeepKeepUnavailable(Exception):
    """Raised when the DeepKeep firewall API cannot be reached or errors out."""


def _is_warming_up(resp: httpx.Response) -> bool:
    """True when DeepKeep is spinning a hibernated firewall back up."""
    if resp.status_code != 503:
        return False
    body = resp.text.lower()
    return any(marker in body for marker in WARMUP_MARKERS)


async def _call_deepkeep(
    path: str, firewall_id: str, field_name: Literal["input", "output"], text: str
) -> dict[str, Any]:
    payload = {"model": firewall_id, field_name: text}
    last_error: str | None = None

    for attempt in range(DEEPKEEP_WARMUP_RETRIES + 1):
        try:
            resp = await client.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise DeepKeepUnavailable(
                f"DeepKeep request failed for {path}: {exc}"
            ) from exc

        if _is_warming_up(resp) and attempt < DEEPKEEP_WARMUP_RETRIES:
            wait = DEEPKEEP_WARMUP_BACKOFF_SECONDS * (attempt + 1)
            logger.warning(
                "DeepKeep firewall warming up (attempt %d/%d), retrying in %.1fs",
                attempt + 1,
                DEEPKEEP_WARMUP_RETRIES,
                wait,
            )
            last_error = resp.text.strip()
            await asyncio.sleep(wait)
            continue

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise DeepKeepUnavailable(
                f"DeepKeep returned HTTP {exc.response.status_code} for {path}: "
                f"{resp.text.strip()[:200]}"
            ) from exc

        try:
            return resp.json()
        except ValueError as exc:  # non-JSON body
            raise DeepKeepUnavailable(
                f"DeepKeep returned non-JSON body for {path}"
            ) from exc

    raise DeepKeepUnavailable(
        f"DeepKeep firewall still warming up after {DEEPKEEP_WARMUP_RETRIES} retries: "
        f"{(last_error or '')[:200]}"
    )


def _log_verbosity_actions(verbosity: list[dict[str, Any]], direction: str) -> None:
    """Log each fired guardrail and its action for debugging."""
    if not verbosity:
        logger.info("[%s] no guardrails fired (empty verbosity)", direction)
        return
    for v in verbosity:
        name = v.get("guardrail_name", "<unknown>")
        action = v.get("details", {}).get("guardrail_action", "allow")
        logger.info("[%s] guardrail=%r action=%r", direction, name, action)


def _winning_action_and_name(
    verbosity: list[dict[str, Any]],
) -> tuple[str, str | None]:
    """
    Return (action, guardrail_name) for the first-listed non-allow rail.

    DeepKeep documents that firewall order determines precedence: the first
    listed guardrail that fired wins, even if a later one is more severe
    (e.g. PII replace before Adversarial block).
    """
    for v in verbosity:
        action = v.get("details", {}).get("guardrail_action", "allow")
        if action in ("allow", "", None):
            continue
        return action, v.get("guardrail_name")
    return "allow", None


def _modified_text_for_winner(
    verbosity: list[dict[str, Any]], action: str
) -> str | None:
    """If the winning action is redact/modify, take that rail's modified content."""
    if action not in ("redact", "modify"):
        return None
    for v in verbosity:
        details = v.get("details", {})
        rail_action = details.get("guardrail_action", "allow")
        if rail_action in ("allow", "", None):
            continue
        if rail_action in ("redact", "modify"):
            modified = details.get("modified") or []
            if modified:
                return modified[-1].get("content")
        return None
    return None


def _mutate_response(
    *,
    verdict: bool,
    transformed: bool,
    result: dict[str, Any],
    message: str | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    """HTTP 200 MutateGuardrailResponse the gateway actually reads."""
    payload: dict[str, Any] = {
        "verdict": verdict,
        "transformed": transformed,
        "result": result,
    }
    if message:
        payload["message"] = message
    if extra:
        payload.update(extra)
    return JSONResponse(status_code=200, content=payload)


def _passthrough(body: dict[str, Any]) -> JSONResponse:
    """Allow the request/response through unchanged (no mutation)."""
    return _mutate_response(verdict=True, transformed=False, result=body)


def _deny_response(
    body: dict[str, Any],
    dk_result: dict[str, Any],
    guardrail_name: str | None,
) -> JSONResponse:
    name = guardrail_name or "<unknown>"
    message = f"Request blocked by DeepKeep AI Firewall ({name})"
    return _mutate_response(
        verdict=False,
        transformed=False,
        result=body,
        message=message,
        extra={
            "request_id": dk_result.get("request_id", ""),
            "risk_level": dk_result.get("risk_level", ""),
            "guardrail_name": name,
        },
    )


def _unavailable_response(
    exc: Exception, direction: str, body: dict[str, Any]
) -> Response:
    """Handle DeepKeep being unreachable, honouring DEEPKEEP_FAIL_OPEN."""
    if DEEPKEEP_FAIL_OPEN:
        logger.error("[%s] DeepKeep unavailable (%s) → failing open", direction, exc)
        return _passthrough(body)

    logger.error("[%s] DeepKeep unavailable (%s) → failing closed (HTTP 503)", direction, exc)
    return JSONResponse(
        status_code=503,
        content={
            "error": "DeepKeep AI Firewall unavailable",
            "detail": str(exc),
        },
    )


def _decide_verdict(
    dk_result: dict[str, Any], direction: str
) -> tuple[str, str | None]:
    """
    Classify DeepKeep result into allow | redact | modify | block.

    Returns (action, blocker_name). action is "allow" for pass-through
    (flagged=false or worst is allow/alert).
    """
    verbosity = dk_result.get("verbosity") or []
    _log_verbosity_actions(verbosity, direction)

    if not dk_result.get("flagged"):
        logger.info("[%s] flagged=false → pass-through", direction)
        return "allow", None

    action, name = _winning_action_and_name(verbosity)
    if action in ("allow", "alert"):
        logger.info(
            "[%s] winning_action=%r name=%r → pass-through", direction, action, name
        )
        return "allow", None

    logger.info("[%s] winning_action=%r name=%r", direction, action, name)
    return action, name


# ---------------------------------------------------------------------------
# Input guardrail (pre-moderation) — called on Target: Request
# ---------------------------------------------------------------------------
@app.post("/guardrails/input", dependencies=[Depends(require_bearer)])
async def input_guardrail(request: Request):
    body = await request.json()
    request_body = copy.deepcopy(body.get("requestBody") or {})
    messages = list(request_body.get("messages", []))

    text, idx = _extract_last_text(messages, "user")
    if not text:
        return _passthrough(request_body)

    try:
        dk_result = await _call_deepkeep(
            "/api/v3/openai/moderations/pre",
            DEEPKEEP_INPUT_FIREWALL_ID,
            "input",
            text,
        )
    except DeepKeepUnavailable as exc:
        return _unavailable_response(exc, "input", request_body)

    action, blocker = _decide_verdict(dk_result, "input")

    if action == "allow":
        return _passthrough(request_body)

    if action == "block":
        return _deny_response(request_body, dk_result, blocker)

    # redact / modify — splice modified content back into the original messages
    modified_text = _modified_text_for_winner(
        dk_result.get("verbosity") or [], action
    )
    if modified_text is not None and idx >= 0:
        messages[idx] = {**messages[idx], "content": modified_text}
        request_body["messages"] = messages
        logger.info("[input] mutating requestBody with redacted/modified text")
        return _mutate_response(
            verdict=True,
            transformed=True,
            result=request_body,
            extra={
                "request_id": dk_result.get("request_id", ""),
                "risk_level": dk_result.get("risk_level", ""),
            },
        )

    logger.warning(
        "[input] redact/modify without usable modified text → pass-through"
    )
    return _passthrough(request_body)


# ---------------------------------------------------------------------------
# Output guardrail (post-moderation) — called on Target: Response
# ---------------------------------------------------------------------------
@app.post("/guardrails/output", dependencies=[Depends(require_bearer)])
async def output_guardrail(request: Request):
    body = await request.json()
    response_body = copy.deepcopy(body.get("responseBody") or {})
    choices = list(response_body.get("choices", []))

    if not choices:
        return _passthrough(response_body)

    message = dict(choices[0].get("message") or {})
    text = message.get("content", "")
    if not text:
        return _passthrough(response_body)

    try:
        dk_result = await _call_deepkeep(
            "/api/v3/openai/moderations/post",
            DEEPKEEP_OUTPUT_FIREWALL_ID,
            "output",
            text,
        )
    except DeepKeepUnavailable as exc:
        return _unavailable_response(exc, "output", response_body)

    action, blocker = _decide_verdict(dk_result, "output")

    if action == "allow":
        return _passthrough(response_body)

    if action == "block":
        return _deny_response(response_body, dk_result, blocker)

    modified_text = _modified_text_for_winner(
        dk_result.get("verbosity") or [], action
    )
    if modified_text is not None:
        message = {**message, "content": modified_text}
        choices[0] = {**choices[0], "message": message}
        response_body["choices"] = choices
        logger.info("[output] mutating responseBody with redacted/modified text")
        return _mutate_response(
            verdict=True,
            transformed=True,
            result=response_body,
            extra={
                "request_id": dk_result.get("request_id", ""),
                "risk_level": dk_result.get("risk_level", ""),
            },
        )

    logger.warning(
        "[output] redact/modify without usable modified text → pass-through"
    )
    return _passthrough(response_body)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/diagnose", dependencies=[Depends(require_bearer)])
async def diagnose():
    """
    Probe DeepKeep connectivity and classify the failure.

    Distinguishes host-down (5xx / connection error) from auth problems (401/403)
    and bad firewall id (400/404), so misconfiguration is easy to tell apart from
    a DeepKeep outage.
    """
    result: dict[str, Any] = {
        "base_url": DEEPKEEP_BASE_URL,
        "input_firewall_id": DEEPKEEP_INPUT_FIREWALL_ID,
        "api_key_present": bool(DEEPKEEP_API_KEY),
        "api_key_length": len(DEEPKEEP_API_KEY or ""),
    }

    try:
        resp = await client.post(
            "/api/v3/openai/moderations/pre",
            json={"model": DEEPKEEP_INPUT_FIREWALL_ID, "input": "connectivity probe"},
        )
    except httpx.HTTPError as exc:
        result["reachable"] = False
        result["verdict"] = "host_unreachable"
        result["detail"] = str(exc)
        return result

    status = resp.status_code
    result["reachable"] = True
    result["status_code"] = status
    result["server"] = resp.headers.get("server")

    if status < 300:
        result["verdict"] = "ok"
    elif status in (401, 403):
        result["verdict"] = "auth_failed_check_api_key"
    elif status in (400, 404, 422):
        result["verdict"] = "request_rejected_check_firewall_id_or_payload"
    elif _is_warming_up(resp):
        result["verdict"] = "firewall_warming_up_retry_shortly"
    elif status >= 500:
        result["verdict"] = "deepkeep_service_down"
    else:
        result["verdict"] = "unexpected_status"

    result["body_preview"] = resp.text[:300]
    return result
