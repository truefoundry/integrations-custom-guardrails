"""RepelloAI Argus client and validate rail handlers.

Scanning and verdict mapping live here; the redact rails in `redact.py` reuse
the same scan and apply `masked_result`. See docs/DESIGN.md.

Upstream failures always raise so the gateway's `Fail on error` setting decides
the outcome. Argus response bodies are never logged: `details.text` holds the
detected secret or PII in plaintext, and `masked_result` echoes the scanned
content.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import httpx

from entities import (
    InputGuardrailRequest,
    OutputGuardrailRequest,
    ValidateGuardrailResponse,
)
from guardrail._helpers import ContentUnit, input_units, output_units

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://argusapi.repello.ai/sdk/v1"

# Below the gateway's 10s budget so the wrapper times out first and reports a
# usable error.
DEFAULT_TIMEOUT_S = 6.0

DEFAULT_MAX_TEXT_CHARS = 20_000

# Argus only validates the asset ID when save is true. With save=false a typo'd
# asset silently passes all traffic. Quota is unaffected either way.
SAVE_EVENTS = True

VALID_VERDICTS = frozenset({"passed", "flagged", "blocked"})
BLOCK_ACTION = "block"

PROMPT_RAIL = ("analyze/prompt", "prompt")
RESPONSE_RAIL = ("analyze/response", "response")


class ArgusApiError(Exception):
    """An upstream Argus problem. Surfaces as 5xx so `Fail on error` governs it."""

    def __init__(
        self,
        detail: str,
        *,
        error: str = "argus_upstream_error",
        status_code: int = 503,
    ) -> None:
        self.detail = detail
        self.error = error
        self.status_code = status_code
        super().__init__(detail)


class ArgusRateLimited(ArgusApiError):
    """Argus returned 429 — shared API-key rate limit or exhausted quota."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail, error="argus_rate_limited", status_code=503)


class ArgusNotConfigured(ArgusApiError):
    """Required configuration is missing at request time."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail, error="argus_not_configured", status_code=500)


@dataclass
class UnitResult:
    """One unit's scan result.

    `masked_result` holds redacted content and `unit` holds the text that was
    scanned, so neither this object nor any field of it is ever logged. Only
    `unit.ref`, a locator, is safe to log.
    """

    verdict: str
    unit: ContentUnit
    blocking_policies: list[str] = field(default_factory=list)
    flagged_policies: list[str] = field(default_factory=list)
    masked_result: Optional[str] = None
    redacting_fired: bool = False

    @property
    def is_blocking(self) -> bool:
        """Block on the verdict itself, so a malformed `policies_violated` array
        cannot turn a `blocked` scan into an allow. Policy names only build the
        response message."""
        return self.verdict == "blocked" or bool(self.blocking_policies)


@dataclass
class ScanOutcome:
    blocked: bool
    blocking_policies: list[str]
    flagged_policies: list[str]
    unit_count: int
    results: list[UnitResult] = field(default_factory=list)


# Argus exposes no endpoint listing an asset's configured policies. These
# counters let an operator infer a zero-policy asset: many scans, all `passed`,
# and an empty `observed_policies`.
_stats: dict[str, Any] = {
    "scans_total": 0,
    "passed": 0,
    "flagged": 0,
    "blocked": 0,
    "errors": 0,
    "rate_limited": 0,
    "observed_policies": set(),
}


def stats_snapshot() -> dict[str, Any]:
    snapshot = {k: v for k, v in _stats.items() if k != "observed_policies"}
    snapshot["observed_policies"] = sorted(_stats["observed_policies"])
    return snapshot


def reset_stats() -> None:
    for key in ("scans_total", "passed", "flagged", "blocked", "errors", "rate_limited"):
        _stats[key] = 0
    _stats["observed_policies"] = set()


# --------------------------------------------------------------------------- #
# Configuration (resolved per call, never at import time)
# --------------------------------------------------------------------------- #


def api_base() -> str:
    return os.environ.get("ARGUS_API_BASE", DEFAULT_API_BASE).strip().rstrip("/")


def api_key(config: Optional[dict[str, Any]] = None) -> str:
    """Resolve the Argus API key, preferring dashboard `config.credentials.apiKey`
    over `ARGUS_API_KEY`. The env var is still required at startup so the asset
    check can run before any traffic is served."""
    if isinstance(config, dict):
        credentials = config.get("credentials")
        if isinstance(credentials, dict):
            override = credentials.get("apiKey") or credentials.get("api_key")
            if isinstance(override, str) and override.strip():
                return override.strip()
    key = os.environ.get("ARGUS_API_KEY", "").strip()
    if not key:
        raise ArgusNotConfigured("ARGUS_API_KEY is not set")
    return key


def timeout_seconds() -> float:
    raw = os.environ.get("ARGUS_TIMEOUT_S", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid ARGUS_TIMEOUT_S=%r; using %.1fs", raw, DEFAULT_TIMEOUT_S)
        return DEFAULT_TIMEOUT_S
    return value if value > 0 else DEFAULT_TIMEOUT_S


def max_text_chars() -> int:
    raw = os.environ.get("ARGUS_MAX_TEXT_CHARS", "").strip()
    if not raw:
        return DEFAULT_MAX_TEXT_CHARS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid ARGUS_MAX_TEXT_CHARS=%r; using %d", raw, DEFAULT_MAX_TEXT_CHARS
        )
        return DEFAULT_MAX_TEXT_CHARS
    return value if value > 0 else DEFAULT_MAX_TEXT_CHARS


def resolve_asset_id(config: Optional[dict[str, Any]]) -> str:
    """Resolve the Argus asset, preferring dashboard `config.assetId` over
    `ARGUS_ASSET_ID`. The override lets one deployment serve several policy sets."""
    if isinstance(config, dict):
        override = config.get("assetId") or config.get("asset_id")
        if isinstance(override, str) and override.strip():
            return override.strip()
    asset_id = os.environ.get("ARGUS_ASSET_ID", "").strip()
    if not asset_id:
        raise ArgusNotConfigured(
            "No Argus asset configured. Set ARGUS_ASSET_ID or config.assetId."
        )
    return asset_id


def resolve_user_id(context: Any) -> Optional[str]:
    user = getattr(context, "user", None) or {}
    if not isinstance(user, dict):
        return None
    for key in ("subjectSlug", "subjectId"):
        value = user.get(key)
        if value:
            return str(value)
    return None


def resolve_session_id(context: Any) -> Optional[str]:
    metadata = getattr(context, "metadata", None) or {}
    if not isinstance(metadata, dict):
        return None
    for key in ("request_id", "session_id", "sessionId"):
        value = metadata.get(key)
        if value:
            return str(value)
    return None


# --------------------------------------------------------------------------- #
# HTTP client lifecycle (owned by main.py's lifespan)
# --------------------------------------------------------------------------- #

_client: Optional[httpx.AsyncClient] = None


def init_client() -> httpx.AsyncClient:
    """Create the shared client. Called once from the FastAPI lifespan."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds()),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def get_client() -> httpx.AsyncClient:
    if _client is None:
        raise ArgusApiError(
            "HTTP client not initialised", error="wrapper_not_ready", status_code=500
        )
    return _client


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #


def _policy_names(policies_violated: Any) -> tuple[list[str], list[str]]:
    """Split violated policies into blocking and non-blocking names.

    Only `policy_name` is read. Argus's schema declares `policy_id` and `scope`
    but never emits them, and `details` holds detected secrets in plaintext.
    """
    blocking: list[str] = []
    flagged: list[str] = []
    if not isinstance(policies_violated, list):
        return blocking, flagged

    for policy in policies_violated:
        if not isinstance(policy, dict):
            continue
        name = str(policy.get("policy_name") or "unknown_policy")
        _stats["observed_policies"].add(name)
        # Match "block" exactly; anything else is advisory. Argus emits both
        # "flag" and "flagged" for non-blocking actions.
        if str(policy.get("action_taken", "")).strip().lower() == BLOCK_ACTION:
            blocking.append(name)
        else:
            flagged.append(name)
    return blocking, flagged


def _redacting_fired(policies_violated: Any) -> bool:
    """True when a violated policy is one that redacts.

    Argus attaches a per-policy `masked_result` only to violated policies that
    mask, so key presence identifies them without a policy-name list to
    maintain. `policy_name` on the wire is the customer's dashboard label, not a
    stable key, so matching on names would break on a rename.
    """
    if not isinstance(policies_violated, list):
        return False
    return any(
        isinstance(policy, dict) and "masked_result" in policy
        for policy in policies_violated
    )


async def _scan_unit(
    rail: tuple[str, str],
    unit: ContentUnit,
    *,
    asset_id: str,
    user_id: Optional[str],
    session_id: Optional[str],
    config: Optional[dict[str, Any]] = None,
) -> UnitResult:
    """Scan one standalone content unit. Raises on any upstream problem."""
    endpoint, scan_key = rail
    text = unit.text[: max_text_chars()]

    payload: dict[str, Any] = {
        "asset_id": asset_id,
        "scan_data": {scan_key: text},
        "save": SAVE_EVENTS,
        "metadata": {
            "source": "truefoundry-gateway",
            "unit": unit.kind,
            "ref": unit.ref,
        },
    }
    if user_id:
        payload["user_id"] = user_id
    if session_id:
        payload["session_id"] = session_id

    url = f"{api_base()}/{endpoint}"
    headers = {"X-API-Key": api_key(config), "Content-Type": "application/json"}

    try:
        response = await get_client().post(url, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        _stats["errors"] += 1
        raise ArgusApiError(
            f"Argus request timed out after {timeout_seconds():.1f}s",
            error="argus_timeout",
        ) from exc
    except httpx.HTTPError as exc:
        _stats["errors"] += 1
        raise ArgusApiError(
            f"Failed to reach Argus: {type(exc).__name__}", error="argus_unreachable"
        ) from exc

    if response.status_code == 429:
        _stats["rate_limited"] += 1
        _stats["errors"] += 1
        raise ArgusRateLimited(
            "Argus rate limit or quota exhausted (500 requests/60s per API key, "
            "shared across both scan endpoints)"
        )

    if response.status_code >= 400:
        _stats["errors"] += 1
        # Status code only; the body can echo scanned content.
        raise ArgusApiError(
            f"Argus returned HTTP {response.status_code}",
            error="argus_http_error",
        )

    try:
        body = response.json()
    except ValueError as exc:
        _stats["errors"] += 1
        raise ArgusApiError(
            "Argus returned a non-JSON response", error="argus_bad_response"
        ) from exc

    if not isinstance(body, dict) or "verdict" not in body:
        _stats["errors"] += 1
        raise ArgusApiError(
            "Argus response is missing the verdict field", error="argus_bad_response"
        )

    verdict = str(body.get("verdict", "")).strip().lower()
    if verdict not in VALID_VERDICTS:
        _stats["errors"] += 1
        # An unknown verdict is an error, not an allow.
        raise ArgusApiError(
            f"Argus returned an unrecognized verdict: {verdict!r}",
            error="unrecognized_verdict",
        )

    policies_violated = body.get("policies_violated")
    blocking, flagged = _policy_names(policies_violated)

    _stats["scans_total"] += 1
    _stats[verdict] = _stats.get(verdict, 0) + 1

    masked = body.get("masked_result")

    return UnitResult(
        verdict=verdict,
        unit=unit,
        blocking_policies=blocking,
        flagged_policies=flagged,
        masked_result=masked if isinstance(masked, str) else None,
        redacting_fired=_redacting_fired(policies_violated),
    )


def _first_error(errors: Iterable[BaseException]) -> BaseException:
    """Prefer the most actionable failure when several units fail at once."""
    errors = list(errors)
    for exc in errors:
        if isinstance(exc, ArgusRateLimited):
            return exc
    for exc in errors:
        if isinstance(exc, ArgusApiError):
            return exc
    return errors[0]


async def scan_units(
    units: list[ContentUnit],
    rail: tuple[str, str],
    *,
    asset_id: str,
    user_id: Optional[str],
    session_id: Optional[str],
    config: Optional[dict[str, Any]] = None,
) -> ScanOutcome:
    """Scan every unit concurrently and aggregate: any block wins."""
    if not units:
        return ScanOutcome(
            blocked=False, blocking_policies=[], flagged_policies=[], unit_count=0
        )

    scanned = await asyncio.gather(
        *(
            _scan_unit(
                rail,
                unit,
                asset_id=asset_id,
                user_id=user_id,
                session_id=session_id,
                config=config,
            )
            for unit in units
        ),
        return_exceptions=True,
    )

    failures = [r for r in scanned if isinstance(r, BaseException)]
    if failures:
        raise _first_error(failures)

    results = [r for r in scanned if isinstance(r, UnitResult)]

    blocking: list[str] = []
    flagged: list[str] = []
    blocked = False
    for result in results:
        blocked = blocked or result.is_blocking
        blocking.extend(result.blocking_policies)
        flagged.extend(result.flagged_policies)

    return ScanOutcome(
        blocked=blocked,
        blocking_policies=_dedupe(blocking),
        flagged_policies=_dedupe(flagged),
        unit_count=len(units),
        results=results,
    )


def _dedupe(names: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for name in names:
        seen.setdefault(name, None)
    return list(seen)


def _to_response(outcome: ScanOutcome, direction: str) -> ValidateGuardrailResponse:
    if outcome.blocked:
        # Policy names only; `details` carries matched secrets verbatim.
        policies = ", ".join(outcome.blocking_policies) or "policy violation"
        return ValidateGuardrailResponse(
            verdict=False,
            message=f"Blocked by RepelloAI Argus ({direction}): {policies}",
        )
    if outcome.flagged_policies:
        logger.info(
            "Argus flagged %s content without blocking: %s",
            direction,
            ", ".join(outcome.flagged_policies),
        )
    return ValidateGuardrailResponse(verdict=True)


# --------------------------------------------------------------------------- #
# Rail handlers
# --------------------------------------------------------------------------- #


async def scan_request(
    request: InputGuardrailRequest | OutputGuardrailRequest,
    direction: str,
) -> ScanOutcome:
    """Extract this direction's content units and scan them all concurrently.

    Shared by the validate and redact rails in both directions. An outcome with
    `unit_count == 0` means nothing was scanned and no Argus call was made;
    callers decide what that means for their operation.
    """
    if direction == "input":
        units = input_units(request.requestBody or {})
        rail = PROMPT_RAIL
    else:
        units = output_units(request.responseBody or {})
        rail = RESPONSE_RAIL

    if not units:
        return ScanOutcome(
            blocked=False, blocking_policies=[], flagged_policies=[], unit_count=0
        )

    logger.info("%s rail scanning %d unit(s): %s", direction, len(units), _kinds(units))
    return await scan_units(
        units,
        rail,
        asset_id=resolve_asset_id(request.config),
        user_id=resolve_user_id(request.context),
        session_id=resolve_session_id(request.context),
        config=request.config,
    )


async def argus_input(request: InputGuardrailRequest) -> ValidateGuardrailResponse:
    """Input rail: scan the latest user message and any new tool results."""
    outcome = await scan_request(request, "input")
    if not outcome.unit_count:
        logger.warning("Input rail found no scannable content; allowing")
    return _to_response(outcome, "input")


async def argus_output(request: OutputGuardrailRequest) -> ValidateGuardrailResponse:
    """Output rail: scan every populated choice and every tool call's arguments."""
    outcome = await scan_request(request, "output")
    if not outcome.unit_count:
        logger.warning("Output rail found no scannable content; allowing")
    return _to_response(outcome, "output")


def _kinds(units: list[ContentUnit]) -> str:
    counts: dict[str, int] = {}
    for unit in units:
        counts[unit.kind] = counts.get(unit.kind, 0) + 1
    return ", ".join(f"{kind}={count}" for kind, count in sorted(counts.items()))


# --------------------------------------------------------------------------- #
# Startup validation
# --------------------------------------------------------------------------- #


async def verify_asset(asset_id: str) -> bool:
    """Check the asset exists before serving traffic. An unverified asset ID is
    invisible at request time: Argus skips evaluation and returns `passed`."""
    url = f"{api_base()}/verify/asset"
    response = await get_client().get(
        url, params={"asset_id": asset_id}, headers={"X-API-Key": api_key()}
    )
    if response.status_code >= 400:
        raise ArgusApiError(
            f"Argus asset verification returned HTTP {response.status_code}",
            error="argus_asset_verification_failed",
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise ArgusApiError(
            "Argus asset verification returned non-JSON",
            error="argus_asset_verification_failed",
        ) from exc
    return bool(isinstance(body, dict) and body.get("valid"))
