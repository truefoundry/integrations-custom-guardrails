"""Run the wrapper locally under uvicorn and send it direct calls, gateway payload shape included.

Starts uvicorn on a free 127.0.0.1 port with LOG_PAYLOADS=false and the wrapper's log
going to a file, sends one request per case (15 s timeout, no retries), stops the
server, and prints how many times the Bigeye key appears in that log. Reads BIGEYE_*
from integrations/bigeye/.env. The key is never printed, and the wrapper process is
started without it, as in production.

Expectations don't depend on what the catalog holds: the SQL case checks that the
verdict matches block_on for whichever decision Bigeye returns, and the next case
forces a block by setting block_on to that decision.

Usage (from integrations/bigeye):
    .venv/bin/python devtools/local_wrapper_check.py --scheme apikey [--log wrapper-local.log]
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx
from dotenv import load_dotenv

INTEGRATION_DIR = Path(__file__).resolve().parent.parent
REQUIRED_ENV = ("BIGEYE_URL", "BIGEYE_API_KEY", "BIGEYE_WORKSPACE_ID", "BIGEYE_SOURCE_NAME", "BIGEYE_TEST_SQL")
REQUEST_TIMEOUT_S = 15
STARTUP_TIMEOUT_S = 20
STARTUP_POLL_S = 0.25
LOG_SETTLE_S = 0.3
SHUTDOWN_TIMEOUT_S = 10
KEY_PREFIX = "bigeye_pak_"
# Well-formed (alphanumeric, real length) so Bigeye answers 401; a malformed key gets a base64 500 instead.
WRONG_KEY_FILL = "TfyLocalCheckWrongKey0123456789"
DEFAULT_KEY_SUFFIX_LEN = 40
OUTBOUND_MARKER = "HTTP Request:"
REJECTED_KEY_LOG = "Bigeye rejected the API key"
MASK = "***"
UNSPECIFIED = "UNSPECIFIED"
DECISIONS = ("ALLOW", "WARN", "REQUIRE_CONFIRMATION", "DENY", UNSPECIFIED)
DEFAULT_BLOCK_ON = ("DENY", "REQUIRE_CONFIRMATION")


@dataclass
class Case:
    name: str
    expected: str
    send: Callable[[httpx.Client], httpx.Response]
    check: Callable[[httpx.Response, str], list]


def load_env() -> dict:
    load_dotenv(INTEGRATION_DIR / ".env")
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k, "").strip()]
    if missing:
        sys.exit(f"Missing env vars: {', '.join(missing)} (set them in integrations/bigeye/.env)")
    return {k: os.environ[k].strip() for k in REQUIRED_ENV}


def wrong_key(api_key: str) -> str:
    suffix_len = len(api_key) - len(KEY_PREFIX) if api_key.startswith(KEY_PREFIX) else DEFAULT_KEY_SUFFIX_LEN
    fill = WRONG_KEY_FILL * (suffix_len // len(WRONG_KEY_FILL) + 1)
    key = KEY_PREFIX + fill[:suffix_len]
    if key == api_key:
        raise RuntimeError("generated wrong key matches the real key")
    return key


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wrapper_env() -> dict:
    """The wrapper's env: no BIGEYE_* smoke-test values, and an empty key so main.py's load_dotenv can't add it.

    BIGEYE_ALLOWED_HOSTS passes through only when set; otherwise the wrapper's default applies.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("BIGEYE_")}
    env.update(BIGEYE_API_KEY="", LOG_PAYLOADS="false", LOG_LEVEL="info", PYTHONUNBUFFERED="1")
    allowed_hosts = os.environ.get("BIGEYE_ALLOWED_HOSTS", "").strip()
    if allowed_hosts:
        env["BIGEYE_ALLOWED_HOSTS"] = allowed_hosts
    return env


def gateway_payload(base: str, arguments: dict, dashboard_config: dict) -> dict:
    """Phase 1 shape: requestBody is the tool arguments; config is {url, config}; context.user from the token."""
    return {
        "requestBody": arguments,
        "config": {"url": f"{base}/mcp-pre-invoke", "config": dashboard_config},
        "context": {
            "user": {
                "subjectId": "local-check-user",
                "subjectType": "user",
                "subjectSlug": "local-wrapper-check",
                "tenantName": "local",
            },
            "metadata": {},
        },
    }


def expect(condition: bool, label: str) -> list:
    return [] if condition else [label]


def body_of(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def expected_verdict(summary: dict, block_on: tuple) -> bool:
    """The README's block rule, recomputed from the response summary rather than taken from the wrapper."""
    if summary.get("overall_decision") in block_on:
        return False
    undecided = bool(summary.get("unresolved_count")) or bool(summary.get("uncataloged_count"))
    return not (UNSPECIFIED in block_on and undecided)


def build_cases(base: str, env: dict, scheme: str) -> list:
    api_key = env["BIGEYE_API_KEY"]
    dashboard = {
        "bigeye_url": env["BIGEYE_URL"],
        "workspace_id": int(env["BIGEYE_WORKSPACE_ID"]),
        "auth_scheme": scheme,
        "source_name": env["BIGEYE_SOURCE_NAME"],
    }
    run_query = {"query": env["BIGEYE_TEST_SQL"]}

    def rail(arguments: dict, config: dict, key: str = api_key) -> Callable[[httpx.Client], httpx.Response]:
        payload = gateway_payload(base, arguments, config)
        return lambda http: http.post(f"{base}/mcp-pre-invoke", json=payload, headers={"Authorization": f"Bearer {key}"})

    def summary(r: httpx.Response) -> dict:
        return body_of(r).get("bigeye") or {}

    observed: dict = {}

    def remember_decision(send: Callable[[httpx.Client], httpx.Response]) -> Callable[[httpx.Client], httpx.Response]:
        def wrapped(http: httpx.Client) -> httpx.Response:
            response = send(http)
            observed["decision"] = summary(response).get("overall_decision")
            return response

        return wrapped

    def block_on_observed_decision(http: httpx.Client) -> httpx.Response:
        decision = observed.get("decision")
        block_on = [decision] if decision in DECISIONS else []
        return rail(run_query, {**dashboard, "block_on": block_on})(http)

    def names_observed_decision(r: httpx.Response) -> bool:
        return str(body_of(r).get("message") or "").startswith(f"Bigeye {observed.get('decision')}")

    return [
        Case(
            "GET /health",
            "200, status ok",
            lambda http: http.get(f"{base}/health"),
            lambda r, log: expect(r.status_code == 200 and body_of(r) == {"status": "ok"}, "health"),
        ),
        Case(
            "GET /debug/loaded-config",
            "200, log_payloads false",
            lambda http: http.get(f"{base}/debug/loaded-config"),
            lambda r, log: expect(r.status_code == 200 and body_of(r).get("log_payloads") is False, "debug config"),
        ),
        Case(
            "run_query SQL, default block_on",
            "200, checked true, a known decision, verdict matches the default block_on for that decision",
            remember_decision(rail(run_query, dashboard)),
            lambda r, log: expect(r.status_code == 200, "status")
            + expect(summary(r).get("checked") is True, "checked")
            + expect(summary(r).get("overall_decision") in DECISIONS, "decision")
            + expect(isinstance(summary(r).get("assets_found"), int), "assets_found")
            + expect(body_of(r).get("verdict") is expected_verdict(summary(r), DEFAULT_BLOCK_ON), "verdict matches block_on")
            + expect(OUTBOUND_MARKER in log, "Bigeye was called"),
        ),
        Case(
            "run_query SQL, block_on = [the decision Bigeye just returned]",
            "200, verdict false, message names that decision",
            block_on_observed_decision,
            lambda r, log: expect(observed.get("decision") in DECISIONS, "decision from the previous case")
            + expect(r.status_code == 200, "status")
            + expect(body_of(r).get("verdict") is False, "verdict")
            + expect(names_observed_decision(r), "message")
            + expect(OUTBOUND_MARKER in log, "Bigeye was called"),
        ),
        Case(
            "no SQL, tables or columns",
            "200, verdict true, checked false, no Bigeye call",
            rail({"limit": 10}, dashboard),
            lambda r, log: expect(r.status_code == 200, "status")
            + expect(body_of(r).get("verdict") is True, "verdict")
            + expect(summary(r).get("checked") is False, "checked")
            + expect(OUTBOUND_MARKER not in log, "no outbound call"),
        ),
        Case(
            "wrong key (well-formed)",
            '502, "Bigeye rejected the API key" logged',
            rail(run_query, dashboard, key=wrong_key(api_key)),
            lambda r, log: expect(r.status_code == 502, "status") + expect(REJECTED_KEY_LOG in log, "rejected-key log line"),
        ),
        Case(
            "bigeye_url https://example.com",
            "500, no outbound call",
            rail(run_query, {**dashboard, "bigeye_url": "https://example.com"}),
            lambda r, log: expect(r.status_code == 500, "status") + expect(OUTBOUND_MARKER not in log, "no outbound call"),
        ),
    ]


def masked(text: str, secrets: tuple) -> str:
    for secret in secrets:
        text = text.replace(secret, MASK)
    return text


def wait_until_up(http: httpx.Client, base: str, server: subprocess.Popen) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    last_error: httpx.TransportError | None = None
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(f"wrapper exited during startup with code {server.returncode}")
        try:
            if http.get(f"{base}/health").status_code == 200:
                return
        except httpx.TransportError as exc:
            last_error = exc
        time.sleep(STARTUP_POLL_S)
    raise RuntimeError(f"wrapper did not answer /health within {STARTUP_TIMEOUT_S}s") from last_error


def run_cases(http: httpx.Client, cases: list, log_path: Path, secrets: tuple) -> int:
    failures = 0
    for case in cases:
        offset = log_path.stat().st_size
        started = time.perf_counter()
        response = case.send(http)
        latency_ms = round((time.perf_counter() - started) * 1000)
        time.sleep(LOG_SETTLE_S)
        with log_path.open(encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            new_log = f.read()
        missed = case.check(response, new_log)
        failures += bool(missed)
        outbound = new_log.count(OUTBOUND_MARKER)
        print(f"{'PASS' if not missed else 'FAIL'}  {case.name}  ({latency_ms} ms, outbound Bigeye calls: {outbound})")
        print(f"      expected: {case.expected}")
        print(f"      actual:   HTTP {response.status_code} {masked(response.text, secrets)[:600]}")
        if missed:
            print(f"      failed checks: {', '.join(missed)}")
        for line in new_log.splitlines():
            if "bigeye" in line.lower() and "uvicorn.access" not in line:
                print(f"      log: {masked(line, secrets)[:300]}")
    return failures


def stop(server: subprocess.Popen) -> None:
    if server.poll() is not None:
        return
    server.terminate()
    try:
        server.wait(timeout=SHUTDOWN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=SHUTDOWN_TIMEOUT_S)


def key_counts(log_path: Path, api_key: str) -> dict:
    text = log_path.read_text(encoding="utf-8", errors="replace")
    counts = {"full key": text.count(api_key)}
    if api_key.startswith(KEY_PREFIX) and len(api_key) > len(KEY_PREFIX):
        counts["key without bigeye_pak_ prefix"] = text.count(api_key[len(KEY_PREFIX):])
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scheme", choices=("apikey", "bearer"), required=True, help="auth_scheme that worked in the smoke test")
    parser.add_argument("--log", default="wrapper-local.log", help="wrapper log file (gitignored *.log)")
    args = parser.parse_args()

    env = load_env()
    secrets = (env["BIGEYE_API_KEY"],)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    log_path = (INTEGRATION_DIR / args.log).resolve()
    command = [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)]

    print(f"wrapper: {base}  LOG_PAYLOADS=false  log: {log_path}")
    with log_path.open("w", encoding="utf-8") as log_file:
        server = subprocess.Popen(command, cwd=INTEGRATION_DIR, env=wrapper_env(), stdout=log_file, stderr=subprocess.STDOUT)
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_S, follow_redirects=False) as http:
            wait_until_up(http, base, server)
            failures = run_cases(http, build_cases(base, env, args.scheme), log_path, secrets)
    finally:
        stop(server)

    counts = key_counts(log_path, env["BIGEYE_API_KEY"])
    print("\nkey occurrences in wrapper log: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    print(f"{failures} case(s) failed")
    sys.exit(1 if failures or any(counts.values()) else 0)


if __name__ == "__main__":
    main()
