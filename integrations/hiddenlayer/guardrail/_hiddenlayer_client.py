"""HTTP client for HiddenLayer AIDR Detection v2 API."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

from guardrail._helpers import bodies_differ

logger = logging.getLogger(__name__)

DEFAULT_REGION = "us"
REGION_ENDPOINTS = {
    "us": {
        "api_base": "https://api.hiddenlayer.ai",
        "auth_base": "https://auth.hiddenlayer.ai",
    },
    "eu": {
        "api_base": "https://api.eu.hiddenlayer.ai",
        "auth_base": "https://auth.eu.hiddenlayer.ai",
    },
}
DEFAULT_TIMEOUT = 10.0
MIN_TIMEOUT = 1.0
MAX_TIMEOUT = 60.0
REQUEST_EVALUATIONS_PATH = "/detection/v2/request-evaluations"
RESPONSE_EVALUATIONS_PATH = "/detection/v2/response-evaluations"
INTERACTION_EVALUATIONS_PATH = "/detection/v2/interaction-evaluations"
TOKEN_PATH = "/oauth2/token"
RUNTIME_ACTION_HEADER = "HL-Runtime-Action"
DEFAULT_PROVIDER = "truefoundry"
MAX_LOG_BODY_CHARS = 200

MISSING_OUTCOME_MESSAGE = (
    "HiddenLayer guardrail error: interaction-evaluations response missing outcome object"
)

V2_PASS_ACTIONS = frozenset({"NONE"})
V2_DENY_ACTIONS = frozenset({"DETECT", "REDACT", "BLOCK"})

INVALID_CREDENTIALS_MESSAGE = (
    "Invalid HiddenLayer credentials. Verify HIDDENLAYER_CLIENT_ID/HIDDENLAYER_CLIENT_SECRET."
)

FORBIDDEN_MESSAGE = (
    "HiddenLayer API forbidden. Verify HL-Project-Id, credential permissions, and tenant region."
)

_INVALID_CREDENTIAL_PHRASES = (
    "invalid client",
    "invalid credentials",
    "authentication failed",
    "unauthorized",
    "forbidden",
    "access denied",
    "not authorized",
)

_token_lock = threading.Lock()
_token_cache: dict[str, Any] = {
    "access_token": None,
    "cache_key": None,
}


@dataclass(frozen=True)
class InlineEvaluationResult:
    """Response from v2 request/response-evaluations inline endpoints."""

    body: dict[str, Any]
    runtime_action: Optional[str]


class HiddenLayerApiError(Exception):
    """Raised when the HiddenLayer API returns a non-success response."""

    def __init__(self, message: str, status_code: int = 502):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def _get_config_value(config: Optional[dict[str, Any]], *keys: str, default: Any = None) -> Any:
    if not config:
        return default
    value: Any = config
    for key in keys:
        if not isinstance(value, dict):
            return default
        value = value.get(key)
    return value if value is not None else default


# SECURITY: region / host / credential / project resolution is SERVER-SIDE ONLY.
#
# The per-request `config` dict is relayed verbatim from the gateway request body and is NOT
# a trust boundary — anyone who can reach this wrapper can supply arbitrary `config`. Honoring
# config-supplied `api_base`/`auth_base`/`region`/`credentials`/`projectId` would let a caller
# redirect HiddenLayer traffic (and the OAuth client_secret / bearer token) to an arbitrary
# host (SSRF / credential exfiltration) or select a weaker AISec policy. These values are
# therefore resolved exclusively from the operator-controlled service environment. The
# `config` parameter is retained for call-site compatibility but intentionally ignored here.


def _validate_base_url(url: str, *, field: str) -> str:
    """Require an https:// base (operator env override). http:// only with an explicit opt-in."""
    cleaned = url.rstrip("/")
    scheme = urlparse(cleaned).scheme
    if scheme == "https":
        return cleaned
    if scheme == "http" and os.getenv("HIDDENLAYER_ALLOW_INSECURE_BASE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return cleaned
    raise HTTPException(
        status_code=500,
        detail=(
            f"Refusing non-HTTPS HiddenLayer {field} '{cleaned}'. Use an https:// base, or set "
            "HIDDENLAYER_ALLOW_INSECURE_BASE=true for local testing only."
        ),
    )


def resolve_region(config: Optional[dict[str, Any]] = None) -> str:
    region = os.getenv("HIDDENLAYER_REGION", DEFAULT_REGION)
    normalized = str(region).strip().lower()
    if normalized not in REGION_ENDPOINTS:
        raise HTTPException(
            status_code=500,
            detail=f"Unsupported HiddenLayer region '{region}'. Use 'us' or 'eu'.",
        )
    return normalized


def resolve_api_base(config: Optional[dict[str, Any]] = None) -> str:
    override = os.getenv("HIDDENLAYER_API_BASE", "").strip()
    if override:
        return _validate_base_url(override, field="api_base")
    return REGION_ENDPOINTS[resolve_region(config)]["api_base"]


def resolve_auth_base(config: Optional[dict[str, Any]] = None) -> str:
    override = os.getenv("HIDDENLAYER_AUTH_BASE", "").strip()
    if override:
        return _validate_base_url(override, field="auth_base")
    return REGION_ENDPOINTS[resolve_region(config)]["auth_base"]


def resolve_client_credentials(config: Optional[dict[str, Any]] = None) -> tuple[str, str]:
    client_id = os.getenv("HIDDENLAYER_CLIENT_ID", "").strip()
    client_secret = os.getenv("HIDDENLAYER_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise HTTPException(
            status_code=500,
            detail=(
                "HiddenLayer credentials not configured. Set HIDDENLAYER_CLIENT_ID "
                "and HIDDENLAYER_CLIENT_SECRET."
            ),
        )
    return client_id, client_secret


def resolve_project_id(config: Optional[dict[str, Any]] = None) -> Optional[str]:
    project_id = os.getenv("HIDDENLAYER_PROJECT_ID", "").strip()
    return project_id or None


def ensure_project_id(config: Optional[dict[str, Any]]) -> str:
    if os.getenv("HIDDENLAYER_SKIP_PROJECT_ID_CHECK", "").strip().lower() in ("1", "true", "yes"):
        return resolve_project_id(config) or "default"

    project_id = resolve_project_id(config)
    if not project_id:
        raise HTTPException(
            status_code=500,
            detail=(
                "HiddenLayer project ID not configured. Set HIDDENLAYER_PROJECT_ID in the service "
                "environment. This selects the AISec policy that controls detection and redaction."
            ),
        )
    return project_id


def resolve_provider(config: Optional[dict[str, Any]]) -> str:
    provider = _get_config_value(config, "provider") or os.getenv("HIDDENLAYER_PROVIDER", DEFAULT_PROVIDER)
    return str(provider).strip() or DEFAULT_PROVIDER


def resolve_timeout(config: Optional[dict[str, Any]]) -> float:
    timeout = _get_config_value(config, "timeout")
    if timeout is None:
        timeout = os.getenv("HIDDENLAYER_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT))
    try:
        value = float(timeout)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Invalid HIDDENLAYER_TIMEOUT_SECONDS or config.timeout: {timeout!r}",
        ) from exc
    return max(MIN_TIMEOUT, min(MAX_TIMEOUT, value))


def resolve_fail_open_on_unavailable(config: Optional[dict[str, Any]]) -> bool:
    """Whether to allow traffic through when HiddenLayer is unavailable (5xx/timeout).

    DEFAULT: fail OPEN (allow). Precedence: env HIDDENLAYER_FAIL_OPEN_ON_UNAVAILABLE wins,
    then per-request config `fail_open_on_unavailable`, else default True. To fail CLOSED
    (block on outage) set either the env var or config to false.

    Security note: with the default, a HiddenLayer outage means traffic passes UNSCANNED.
    Set fail-closed for safety-critical rails where an outage should block.
    """
    env_val = os.getenv("HIDDENLAYER_FAIL_OPEN_ON_UNAVAILABLE", "").strip().lower()
    if env_val in ("1", "true", "yes"):
        return True
    if env_val in ("0", "false", "no"):
        return False
    return bool(_get_config_value(config, "fail_open_on_unavailable", default=True))


def resolve_allow_detect_on_validate(config: Optional[dict[str, Any]]) -> bool:
    for env_key in ("HIDDENLAYER_ALLOW_DETECT_ON_VALIDATE", "HIDDENLAYER_ALLOW_ALERT_ON_VALIDATE"):
        env_val = os.getenv(env_key, "").strip().lower()
        if env_val in ("1", "true", "yes"):
            return True
        if env_val in ("0", "false", "no"):
            return False
    return bool(
        _get_config_value(config, "allow_detect_on_validate")
        or _get_config_value(config, "allow_alert_on_validate", default=False)
    )


def _credentials_cache_key(client_id: str, client_secret: str, auth_base: str) -> str:
    # Hash the credential portion so the plaintext client_secret is never held in a
    # long-lived in-memory key (avoids exposure via heap/core dumps). auth_base is not
    # secret and is kept readable. The digest still uniquely keys distinct credentials.
    digest = hashlib.sha256(f"{client_id}:{client_secret}".encode()).hexdigest()
    return f"{auth_base}:{digest}"


def _invalidate_token_cache() -> None:
    _token_cache["access_token"] = None
    _token_cache["cache_key"] = None


TOKEN_FORM_BODY = "grant_type=client_credentials"


def _fetch_access_token(client_id: str, client_secret: str, auth_base: str, timeout: float) -> str:
    url = f"{auth_base}{TOKEN_PATH}"
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                url,
                auth=(client_id, client_secret),
                content=TOKEN_FORM_BODY,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
    except httpx.RequestError as exc:
        logger.error("HiddenLayer token request failed: %s", exc)
        raise HiddenLayerApiError(f"Failed to connect to HiddenLayer auth API: {exc}") from exc

    if response.status_code == 401:
        raise HiddenLayerApiError(INVALID_CREDENTIALS_MESSAGE, status_code=401)
    if response.status_code == 403:
        raise HiddenLayerApiError(FORBIDDEN_MESSAGE, status_code=403)
    if not response.is_success:
        raise HiddenLayerApiError(
            f"HiddenLayer auth API returned {response.status_code}: {response.text.strip()}",
            status_code=502 if response.status_code >= 500 else response.status_code,
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise HiddenLayerApiError(f"Invalid JSON from HiddenLayer auth API: {exc}") from exc

    token = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise HiddenLayerApiError("HiddenLayer auth API response missing access_token")
    return token.strip()


def get_access_token(config: Optional[dict[str, Any]], *, force_refresh: bool = False) -> str:
    client_id, client_secret = resolve_client_credentials(config)
    auth_base = resolve_auth_base(config)
    timeout = resolve_timeout(config)
    cache_key = _credentials_cache_key(client_id, client_secret, auth_base)

    with _token_lock:
        if (
            not force_refresh
            and _token_cache.get("access_token")
            and _token_cache.get("cache_key") == cache_key
        ):
            return _token_cache["access_token"]

    token = _fetch_access_token(client_id, client_secret, auth_base, timeout)

    with _token_lock:
        if (
            not force_refresh
            and _token_cache.get("access_token")
            and _token_cache.get("cache_key") == cache_key
        ):
            return _token_cache["access_token"]
        _token_cache["access_token"] = token
        _token_cache["cache_key"] = cache_key
        return token


def _parse_error_body(response: httpx.Response) -> Optional[dict[str, Any]]:
    try:
        data = response.json()
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def _normalize_v2_action(action: Any) -> str:
    return str(action or "NONE").strip().upper()


def _runtime_action(response: httpx.Response) -> Optional[str]:
    value = response.headers.get(RUNTIME_ACTION_HEADER)
    if value is None:
        return None
    normalized = str(value).strip().upper()
    return normalized or None


def _safe_response_snippet(response: httpx.Response, limit: int = MAX_LOG_BODY_CHARS) -> str:
    text = (response.text or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... (truncated)"


def _body_indicates_invalid_credentials(
    status_code: int,
    body_text: str,
    body_json: Optional[dict[str, Any]],
) -> bool:
    if status_code == 401:
        return True
    if status_code == 403:
        return False
    haystack = body_text.lower()
    if any(phrase in haystack for phrase in _INVALID_CREDENTIAL_PHRASES):
        return True
    if not body_json:
        return False
    for field in ("message", "error", "detail", "description", "title"):
        value = body_json.get(field)
        if isinstance(value, str) and any(phrase in value.lower() for phrase in _INVALID_CREDENTIAL_PHRASES):
            return True
    return False


def _hiddenlayer_http_error(response: httpx.Response) -> HiddenLayerApiError:
    body_text = response.text or ""
    body_json = _parse_error_body(response)

    if _body_indicates_invalid_credentials(response.status_code, body_text, body_json):
        return HiddenLayerApiError(INVALID_CREDENTIALS_MESSAGE, status_code=401)
    if response.status_code == 403:
        return HiddenLayerApiError(FORBIDDEN_MESSAGE, status_code=403)

    if body_json:
        for field in ("message", "detail", "error", "description", "title"):
            value = body_json.get(field)
            if isinstance(value, str) and value.strip():
                return HiddenLayerApiError(
                    f"HiddenLayer API error: {value.strip()}",
                    status_code=502 if response.status_code >= 500 else response.status_code,
                )

    if body_text.strip():
        return HiddenLayerApiError(
            f"HiddenLayer API returned {response.status_code}: {body_text.strip()}",
            status_code=502 if response.status_code >= 500 else response.status_code,
        )

    return HiddenLayerApiError(
        f"HiddenLayer API returned {response.status_code}",
        status_code=502 if response.status_code >= 500 else response.status_code,
    )


def _post_hiddenlayer(
    path: str,
    payload: dict[str, Any],
    config: Optional[dict[str, Any]],
    *,
    session_id: Optional[str] = None,
) -> httpx.Response:
    api_base = resolve_api_base(config)
    timeout = resolve_timeout(config)
    project_id = ensure_project_id(config)
    url = f"{api_base}{path}"

    headers = {
        "Authorization": f"Bearer {get_access_token(config)}",
        "Content-Type": "application/json",
        "HL-Project-Id": project_id,
    }
    if session_id:
        headers["HL-Runtime-Session-Id"] = session_id

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, json=payload, headers=headers)
            if response.status_code == 401:
                _invalidate_token_cache()
                headers["Authorization"] = f"Bearer {get_access_token(config, force_refresh=True)}"
                response = client.post(url, json=payload, headers=headers)
            if response.status_code == 503:
                logger.warning("HiddenLayer API returned 503; retrying once")
                response = client.post(url, json=payload, headers=headers)
    except httpx.RequestError as exc:
        logger.error("HiddenLayer API request failed: %s", exc)
        raise HiddenLayerApiError(f"Failed to connect to HiddenLayer API: {exc}") from exc

    if not response.is_success:
        logger.error(
            "HiddenLayer API error %s: %s",
            response.status_code,
            _safe_response_snippet(response),
        )
        raise _hiddenlayer_http_error(response)
    return response


def call_request_evaluations(
    request_body: dict[str, Any],
    config: Optional[dict[str, Any]],
    *,
    session_id: Optional[str] = None,
) -> InlineEvaluationResult:
    """POST /detection/v2/request-evaluations — inline pre-model scan."""
    response = _post_hiddenlayer(
        REQUEST_EVALUATIONS_PATH,
        request_body,
        config,
        session_id=session_id,
    )
    try:
        body = response.json()
    except ValueError as exc:
        raise HiddenLayerApiError(f"Invalid JSON from HiddenLayer API: {exc}") from exc
    if not isinstance(body, dict):
        raise HiddenLayerApiError("HiddenLayer API response was not a JSON object")
    return InlineEvaluationResult(body=body, runtime_action=_runtime_action(response))


def call_response_evaluations(
    response_body: dict[str, Any],
    config: Optional[dict[str, Any]],
    *,
    session_id: Optional[str] = None,
) -> InlineEvaluationResult:
    """POST /detection/v2/response-evaluations — inline post-model scan."""
    response = _post_hiddenlayer(
        RESPONSE_EVALUATIONS_PATH,
        response_body,
        config,
        session_id=session_id,
    )
    try:
        body = response.json()
    except ValueError as exc:
        raise HiddenLayerApiError(f"Invalid JSON from HiddenLayer API: {exc}") from exc
    if not isinstance(body, dict):
        raise HiddenLayerApiError("HiddenLayer API response was not a JSON object")
    return InlineEvaluationResult(body=body, runtime_action=_runtime_action(response))


def call_interaction_evaluations(
    payload: dict[str, Any],
    config: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """POST /detection/v2/interaction-evaluations — full structured evaluation."""
    response = _post_hiddenlayer(INTERACTION_EVALUATIONS_PATH, payload, config)
    try:
        body = response.json()
    except ValueError as exc:
        raise HiddenLayerApiError(f"Invalid JSON from HiddenLayer API: {exc}") from exc
    if not isinstance(body, dict):
        raise HiddenLayerApiError("HiddenLayer API response was not a JSON object")
    return body


def _parse_outcome(response: dict[str, Any]) -> Optional[dict[str, Any]]:
    outcome = response.get("outcome")
    return outcome if isinstance(outcome, dict) else None


def format_outcome_message(response: dict[str, Any]) -> str:
    outcome = response.get("outcome") or {}
    action = _normalize_v2_action(outcome.get("action"))
    threat_level = str(outcome.get("threat_level") or "Unknown")

    detections = outcome.get("detections") or []
    rule_names: list[str] = []
    for item in detections:
        if isinstance(item, dict):
            name = item.get("rule_name")
            if isinstance(name, str) and name.strip():
                rule_names.append(name.strip())

    if not rule_names:
        for message in (response.get("evaluated_interaction") or {}).get("messages") or []:
            if not isinstance(message, dict):
                continue
            analysis = message.get("analysis")
            signals = analysis.get("signals") if isinstance(analysis, dict) else None
            if not isinstance(signals, dict):
                continue
            prompt_injection = signals.get("prompt_injection")
            if isinstance(prompt_injection, dict) and prompt_injection.get("detected"):
                rule_names.append("prompt_injection")
                break
            pii = signals.get("personally_identifiable_information")
            if isinstance(pii, dict):
                entities = pii.get("entities")
                if isinstance(entities, list) and entities:
                    rule_names.append("personally_identifiable_information")

    detail = "; ".join(rule_names) if rule_names else "policy violation"
    return f"HiddenLayer guardrail {action.lower()}: {threat_level} threat — {detail}"


def _effective_interaction_changed(response: dict[str, Any], original_messages: list[dict[str, Any]]) -> bool:
    """True when effective_interaction text differs from the submitted interaction."""
    from guardrail._helpers import interaction_messages_to_texts

    outcome = response.get("outcome") or {}
    effective = outcome.get("effective_interaction") or {}
    effective_messages = effective.get("messages") or []
    if not isinstance(effective_messages, list) or not effective_messages:
        return False
    return interaction_messages_to_texts(original_messages) != interaction_messages_to_texts(effective_messages)


def map_validate_response(
    response: dict[str, Any],
    *,
    original_messages: list[dict[str, Any]],
    config: Optional[dict[str, Any]] = None,
) -> tuple[bool, Optional[str], str]:
    outcome = _parse_outcome(response)
    if outcome is None:
        logger.error("HiddenLayer interaction-evaluations response missing outcome object")
        return False, MISSING_OUTCOME_MESSAGE, "NONE"

    action = _normalize_v2_action(outcome.get("action"))

    if action == "DETECT" and resolve_allow_detect_on_validate(config):
        return True, None, action
    if action in V2_PASS_ACTIONS:
        if _effective_interaction_changed(response, original_messages):
            return False, format_outcome_message(
                {**response, "outcome": {**outcome, "action": "REDACT"}}
            ), action
        return True, None, action
    if action in V2_DENY_ACTIONS:
        return False, format_outcome_message(response), action

    logger.warning("Unknown HiddenLayer outcome.action '%s'; treating as allow", action)
    return True, None, action


INLINE_VALIDATE_DENY_MESSAGE = (
    "HiddenLayer guardrail redact: sensitive content detected — use mutate rail to apply redaction"
)


def check_inline_validate_enforcement(
    *,
    original_body: dict[str, Any],
    config: Optional[dict[str, Any]],
    session_id: Optional[str],
    phase: str,
) -> tuple[bool, Optional[str]]:
    """Fallback when interaction-evaluations returns NONE but inline endpoints enforced redaction."""
    try:
        if phase == "output":
            result = call_response_evaluations(original_body, config, session_id=session_id)
        else:
            result = call_request_evaluations(original_body, config, session_id=session_id)
    except HiddenLayerApiError:
        raise

    runtime_action = _normalize_v2_action(result.runtime_action) if result.runtime_action else None
    if runtime_action == "BLOCK":
        return False, "HiddenLayer guardrail block: HIGH threat — inline policy block"
    if bodies_differ(original_body, result.body):
        return False, INLINE_VALIDATE_DENY_MESSAGE
    return True, None


def map_inline_mutate_response(
    result: InlineEvaluationResult,
    *,
    original_body: dict[str, Any],
) -> tuple[bool, bool, dict[str, Any], Optional[str]]:
    runtime_action = _normalize_v2_action(result.runtime_action) if result.runtime_action else None
    modified_body = result.body

    if runtime_action == "BLOCK":
        block_body = modified_body if bodies_differ(original_body, modified_body) else original_body
        return False, False, block_body, "HiddenLayer guardrail block: HIGH threat — inline policy block"

    if bodies_differ(original_body, modified_body):
        return True, True, modified_body, None

    return True, False, original_body, None


def handle_hiddenlayer_error(exc: Exception) -> None:
    if isinstance(exc, HiddenLayerApiError):
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    if isinstance(exc, HTTPException):
        raise
    logger.exception("Unexpected HiddenLayer guardrail error")
    raise HTTPException(status_code=500, detail=f"HiddenLayer guardrail error: {exc}") from exc
