"""Live smoke test against Bigeye, using the wrapper's own request builder and parser.

Reads secrets from the environment or integrations/bigeye/.env, never prints them,
and masks the API key in every line it prints. One attempt per call, 15 s timeout;
a network error or timeout raises instead of retrying.

Usage (from integrations/bigeye):
    .venv/bin/python devtools/smoke_test.py                    # auth check, then TABLE, COLUMN, SQL and SQL+policy
    .venv/bin/python devtools/smoke_test.py --list [--scheme bearer]   # stored decisions for the smoke-test agent
    .venv/bin/python devtools/smoke_test.py --snake            # same decisions with snake_case request keys

Required env: BIGEYE_URL, BIGEYE_API_KEY, BIGEYE_WORKSPACE_ID, BIGEYE_SOURCE_NAME,
BIGEYE_CLEAN_TABLE, BIGEYE_FLAGGED_COLUMN, BIGEYE_TEST_SQL.
Optional: BIGEYE_WAREHOUSE_TYPE, BIGEYE_ALLOWED_HOSTS (same allowlist as the wrapper),
BIGEYE_SMOKE_AGENT_ID (default truefoundry-smoke-test, kept apart from the gateway's agent).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

import httpx
from dotenv import load_dotenv

INTEGRATION_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INTEGRATION_DIR))

from guardrail._bigeye_client import ACCESS_DECISION_PATH  # noqa: E402
from guardrail._decision import DecisionError, build_request_body, parse_response  # noqa: E402
from guardrail._extract import AccessTargets  # noqa: E402
from guardrail._settings import BigeyeSettings, ConfigError, parse_settings  # noqa: E402

REQUIRED_ENV = (
    "BIGEYE_URL",
    "BIGEYE_API_KEY",
    "BIGEYE_WORKSPACE_ID",
    "BIGEYE_SOURCE_NAME",
    "BIGEYE_CLEAN_TABLE",
    "BIGEYE_FLAGGED_COLUMN",
    "BIGEYE_TEST_SQL",
)
AGENT_ID_ENV = "BIGEYE_SMOKE_AGENT_ID"
DEFAULT_AGENT_ID = "truefoundry-smoke-test"
OPERATION = "smoke_test"
SMOKE_USER = {"subjectType": "user", "subjectSlug": "bigeye-smoke-test"}
AUTH_SCHEMES = ("apikey", "bearer")  # apikey first: it wins when both are accepted
REQUEST_TIMEOUT_S = 15
REQUEST_TIMEOUT_MS = REQUEST_TIMEOUT_S * 1000
VERIFY_PATH = "/api/v1/personal-api-keys/verify"
LIST_PATH = "/api/v1/access-decision/list"
LIST_RECORDS_SHOWN = 10
RAW_CHARS_SHOWN = 4000
MASK = "***"

_secrets: list = []


def load_env() -> dict:
    load_dotenv(INTEGRATION_DIR / ".env")
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k, "").strip()]
    if missing:
        sys.exit(f"Missing env vars: {', '.join(missing)} (set them in integrations/bigeye/.env)")
    env = {k: os.environ[k].strip() for k in REQUIRED_ENV}
    env[AGENT_ID_ENV] = os.environ.get(AGENT_ID_ENV, "").strip() or DEFAULT_AGENT_ID
    _secrets.append(env["BIGEYE_API_KEY"])
    return env


def masked(text: str) -> str:
    for secret in _secrets:
        text = text.replace(secret, MASK)
    return text


def settings_for(env: dict, scheme: str, evaluate_policy: bool = False) -> BigeyeSettings:
    try:
        return parse_settings(
            {
                "bigeye_url": env["BIGEYE_URL"],
                "workspace_id": env["BIGEYE_WORKSPACE_ID"],
                "auth_scheme": scheme,
                "source_name": env["BIGEYE_SOURCE_NAME"],
                "warehouse_type": os.environ.get("BIGEYE_WAREHOUSE_TYPE") or None,
                "evaluate_policy": evaluate_policy,
                "bigeye_timeout_ms": REQUEST_TIMEOUT_MS,
                "agent_id": env[AGENT_ID_ENV],
                "operation": OPERATION,
            }
        )
    except ConfigError as exc:
        sys.exit(f"Config rejected the same way the wrapper would: {exc}")


def auth_headers(settings: BigeyeSettings, api_key: str) -> dict:
    return {"Authorization": f"{settings.auth_scheme} {api_key}", "Accept": "application/json"}


def to_snake_case(value):
    if isinstance(value, dict):
        return {"".join(f"_{c.lower()}" if c.isupper() else c for c in k): to_snake_case(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_snake_case(v) for v in value]
    return value


def timed(send: Callable[..., httpx.Response], *args, **kwargs) -> tuple:
    started = time.perf_counter()
    response = send(*args, **kwargs)
    return response, round((time.perf_counter() - started) * 1000)


def show(label: str, response: httpx.Response, latency_ms: int, is_decision: bool) -> None:
    """One line of status, decision, unresolved count and latency, then the raw body (key masked)."""
    decision, unresolved = "-", "-"
    if is_decision and response.is_success:
        try:
            parsed = parse_response(response.json())
            decision, unresolved = parsed.overall, str(parsed.unresolved_count)
        except (ValueError, AttributeError, DecisionError) as exc:
            decision = f"unusable ({exc})"
    print(f"   {label}: HTTP {response.status_code}  decision={decision}  unresolved={unresolved}  {latency_ms} ms")
    raw = masked(response.text) or "<empty body>"
    suffix = f" ...<truncated {len(raw) - RAW_CHARS_SHOWN} chars>" if len(raw) > RAW_CHARS_SHOWN else ""
    print(f"      raw: {raw[:RAW_CHARS_SHOWN]}{suffix}")


def check_auth(http: httpx.Client, env: dict) -> str:
    print("1. Auth: key verify, then one access-decision call, per scheme (the key is never printed)")
    table_only = AccessTargets(sql=None, tables=(env["BIGEYE_CLEAN_TABLE"],), columns=())
    accepted = []
    for scheme in AUTH_SCHEMES:
        settings = settings_for(env, scheme)
        headers = auth_headers(settings, env["BIGEYE_API_KEY"])
        verify, latency_ms = timed(http.get, settings.base_url + VERIFY_PATH, headers=headers)
        show(f"{settings.auth_scheme:<6} verify", verify, latency_ms, is_decision=False)
        body = build_request_body(table_only, settings, SMOKE_USER, str(uuid.uuid4()))
        decision, latency_ms = timed(http.post, settings.base_url + ACCESS_DECISION_PATH, json=body, headers=headers)
        show(f"{settings.auth_scheme:<6} access-decision TABLE", decision, latency_ms, is_decision=True)
        if decision.is_success:
            accepted.append(scheme)
    if not accepted:
        sys.exit("   Neither apikey nor Bearer was accepted by access-decision. Stopping.")
    print(f"   -> access-decision accepted: {', '.join(accepted)}; using auth_scheme={accepted[0]}\n")
    return accepted[0]


def run_decisions(http: httpx.Client, env: dict, scheme: str, snake: bool) -> None:
    sql = AccessTargets(sql=env["BIGEYE_TEST_SQL"], tables=(), columns=())
    cases = (
        ("TABLE", AccessTargets(sql=None, tables=(env["BIGEYE_CLEAN_TABLE"],), columns=()), False),
        ("COLUMN", AccessTargets(sql=None, tables=(), columns=(env["BIGEYE_FLAGGED_COLUMN"],)), False),
        ("SQL", sql, False),
        ("SQL evaluatePolicy=true", sql, True),
    )
    print(f"2. Decisions ({'snake_case' if snake else 'camelCase'} request keys, auth_scheme={scheme})")
    for label, targets, evaluate_policy in cases:
        settings = settings_for(env, scheme, evaluate_policy)
        span_id = str(uuid.uuid4())
        body = build_request_body(targets, settings, SMOKE_USER, span_id)
        response, latency_ms = timed(
            http.post,
            settings.base_url + ACCESS_DECISION_PATH,
            json=to_snake_case(body) if snake else body,
            headers=auth_headers(settings, env["BIGEYE_API_KEY"]),
        )
        show(f"{label} (span {span_id})", response, latency_ms, is_decision=True)


def _maybe_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _find(record: dict, *keys: str) -> Optional[object]:
    """Look for a key at the top level, then inside the stored request/response objects."""
    for container in (record, *(_maybe_json(v) for v in record.values())):
        if isinstance(container, dict):
            for key in keys:
                if container.get(key) not in (None, ""):
                    return container[key]
    return None


def list_decisions(http: httpx.Client, env: dict, scheme: str) -> None:
    settings = settings_for(env, scheme)
    agent_id = env[AGENT_ID_ENV]
    body = {"workspaceId": int(env["BIGEYE_WORKSPACE_ID"]), "agentExternalId": agent_id}
    print(f"3. Stored decisions for agent {agent_id} (auth_scheme={scheme})")
    response, latency_ms = timed(
        http.post, settings.base_url + LIST_PATH, json=body, headers=auth_headers(settings, env["BIGEYE_API_KEY"])
    )
    show("list", response, latency_ms, is_decision=False)
    if not response.is_success:
        sys.exit("   list failed")
    records = response.json().get("records") or []
    print(f"   {len(records)} records")
    for record in records[:LIST_RECORDS_SHOWN]:
        if not isinstance(record, dict):
            continue
        print(
            f"   {_find(record, 'createdAt', 'created_at', 'timestamp') or '-'}  "
            f"{_find(record, 'overallDecision', 'overall_decision') or '-'}  "
            f"operation={_find(record, 'operation') or '-'}  "
            f"span={_find(record, 'spanExternalId', 'span_external_id') or '-'}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help=f"list stored decisions for the {AGENT_ID_ENV} agent")
    parser.add_argument("--scheme", choices=AUTH_SCHEMES, default="apikey", help="auth scheme for --list")
    parser.add_argument("--snake", action="store_true", help="send snake_case request keys (diagnostic)")
    args = parser.parse_args()

    env = load_env()
    with httpx.Client(timeout=REQUEST_TIMEOUT_S, follow_redirects=False) as http:
        if args.list:
            list_decisions(http, env, args.scheme)
            return
        scheme = check_auth(http, env)
        run_decisions(http, env, scheme, args.snake)


if __name__ == "__main__":
    main()
