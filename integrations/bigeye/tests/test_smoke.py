"""Tests for the Bigeye MCP pre-invoke rail. Bigeye is mocked with httpx.MockTransport.

Covers: every decision value, block_on overrides (including UNSPECIFIED),
missing decisions (proto3 JSON omits default enums), default and configured
argument names, nothing to check (allow or block), Bigeye failures (401, 500,
timeout, unparseable, unknown decision), the host allowlist (exact and
*.domain entries), both config locations, both response casings, warehouse
type mapping, the request body sent to Bigeye, payload-log redaction, retry safety (nothing the gateway
would retry, one Bigeye call per request), and a leak matrix asserting that no
key, identity value or SQL literal reaches a response, a log line or Bigeye on
any path.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

import httpx
import pytest
from fastapi.testclient import TestClient

from guardrail import _bigeye_client, _settings

API_KEY = "test-bigeye-key"
AUTH = {"Authorization": f"Bearer {API_KEY}"}
CLEAN_TABLE = "DEMO_DB.SALES.ORDERS"
FLAGGED_COLUMN = "DEMO_DB.SALES.CUSTOMERS.EMAIL"
SECRET_SQL = "SELECT email FROM DEMO_DB.SALES.CUSTOMERS WHERE email = 'alice@example.com'"
CONFIG = {
    "bigeye_url": "https://demo.bigeye.com",
    "workspace_id": 355,
    "auth_scheme": "apikey",
    "source_name": "demo-snowflake",
    "warehouse_type": "DATABASE_TYPE_SNOWFLAKE",
    "block_on": ["DENY", "REQUIRE_CONFIRMATION"],
    "evaluate_policy": False,
    "bigeye_timeout_ms": 3500,
    "agent_id": "truefoundry-gateway",
    "operation": "mcp_tool_call",
}
USER = {"subjectId": "u-123", "subjectType": "user", "subjectSlug": "alice@example.com", "tenantName": "acme"}


def gateway_body(
    arguments: dict,
    config: Optional[dict] = None,
    nested: bool = True,
    user: Optional[dict] = None,
) -> dict:
    cfg = CONFIG if config is None else config
    return {
        "requestBody": arguments,
        "config": {"url": "https://wrapper.example.com/mcp-pre-invoke", "config": cfg} if nested else cfg,
        "context": {"user": USER if user is None else user, "metadata": {"tfy_trace_id": "t-1"}},
    }


def asset(
    external_id: str,
    decision: Optional[str],
    object_type: str = "ACCESS_OBJECT_TYPE_COLUMN",
    sensitivities: Optional[int] = None,
    open_issues: Optional[int] = None,
) -> dict:
    """``decision=None`` omits accessDecision, as proto3 JSON does for the default enum value."""
    attributes = []
    if open_issues is not None:
        attributes.append({"key": "numOpenIssues", "value": str(open_issues)})
    if sensitivities is not None:
        attributes.append({"key": "numSensitivities", "value": str(sensitivities)})
    entry: dict = {
        "accessObject": {"type": object_type, "externalId": external_id, "workspaceId": 355},
        "accessObjectAttributes": attributes,
    }
    if decision is not None:
        entry["accessDecision"] = f"ACCESS_DECISION_{decision}"
    return entry


def decision_response(overall: Optional[str], assets: list, unresolved: Optional[list] = None) -> dict:
    body: dict = {"accessObjectDecisions": assets, "unresolvedAccessObjects": unresolved or []}
    if overall is not None:
        body["overallDecision"] = f"ACCESS_DECISION_{overall}"
    return body


def to_snake_case(value):
    if isinstance(value, dict):
        return {
            "".join(f"_{c.lower()}" if c.isupper() else c for c in k): to_snake_case(v) for k, v in value.items()
        }
    if isinstance(value, list):
        return [to_snake_case(v) for v in value]
    return value


class FakeBigeye:
    def __init__(self) -> None:
        self.requests: list = []
        self.reply: Callable[[httpx.Request], httpx.Response] = lambda _: httpx.Response(
            200, json=decision_response("ALLOW", [asset(CLEAN_TABLE, "ALLOW", "ACCESS_OBJECT_TYPE_TABLE")])
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.reply(request)

    def respond(self, status: int = 200, body=None, raw: Optional[bytes] = None) -> None:
        self.reply = lambda _: httpx.Response(status, content=raw) if raw is not None else httpx.Response(
            status, json=body
        )

    def last_body(self) -> dict:
        return json.loads(self.requests[-1].content)


@pytest.fixture(autouse=True)
def runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BIGEYE_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("LOG_PAYLOADS", raising=False)


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    from main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def bigeye(monkeypatch: pytest.MonkeyPatch) -> FakeBigeye:
    fake = FakeBigeye()
    monkeypatch.setattr(_bigeye_client, "_client", httpx.AsyncClient(transport=httpx.MockTransport(fake)))
    return fake


def post(client: TestClient, body: dict, headers: Optional[dict] = None) -> httpx.Response:
    return client.post("/mcp-pre-invoke", json=body, headers=AUTH if headers is None else headers)


# --- basics -----------------------------------------------------------------


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_debug_loaded_config_is_non_secret(client: TestClient) -> None:
    r = client.get("/debug/loaded-config")
    assert r.status_code == 200
    body = r.json()
    assert body["routes"] == {"mcp_pre_invoke": ["/mcp-pre-invoke"]}
    assert body["allowed_hosts"] == ["*.bigeye.com"]
    assert body["log_payloads"] is False
    assert body["defaults"]["auth_scheme"] == "apikey"
    assert body["defaults"]["sql_keys"] == ["query", "sql", "statement"]
    assert body["defaults"]["table_keys"] == ["tables", "table"]
    assert body["defaults"]["column_keys"] == ["columns", "column"]
    assert body["defaults"]["on_nothing_to_check"] == "allow"
    assert API_KEY not in r.text


def test_missing_bearer_returns_401_without_calling_bigeye(client: TestClient, bigeye: FakeBigeye) -> None:
    r = post(client, gateway_body({"table": CLEAN_TABLE}), headers={})
    assert r.status_code == 401
    assert bigeye.requests == []


# --- decisions --------------------------------------------------------------


@pytest.mark.parametrize(
    ("decision", "verdict"),
    [("ALLOW", True), ("WARN", True), ("UNSPECIFIED", True), ("REQUIRE_CONFIRMATION", False), ("DENY", False)],
)
def test_each_decision_value(client: TestClient, bigeye: FakeBigeye, decision: str, verdict: bool) -> None:
    bigeye.respond(body=decision_response(decision, [asset(FLAGGED_COLUMN, decision, sensitivities=1)]))
    r = post(client, gateway_body({"column": FLAGGED_COLUMN}))
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] is verdict
    assert (body["message"] is None) is verdict
    summary = body["bigeye"]
    assert summary["checked"] is True
    assert summary["overall_decision"] == decision
    assert summary["assets"] == [{"name": FLAGGED_COLUMN, "decision": decision}]
    assert summary["unresolved_count"] == 0
    assert isinstance(summary["latency_ms"], int)
    uuid.UUID(summary["span_external_id"])


def test_deny_message_names_asset_and_signals(client: TestClient, bigeye: FakeBigeye) -> None:
    bigeye.respond(
        body=decision_response(
            "DENY",
            [
                asset(FLAGGED_COLUMN, "DENY", sensitivities=1, open_issues=0),
                asset(CLEAN_TABLE, "ALLOW", "ACCESS_OBJECT_TYPE_TABLE", open_issues=0),
            ],
        )
    )
    r = post(client, gateway_body({"column": FLAGGED_COLUMN, "table": CLEAN_TABLE}))
    assert r.json()["message"] == f"Bigeye DENY: {FLAGGED_COLUMN} (sensitive findings: 1, open issues: 0)"


def test_require_confirmation_allowed_when_removed_from_block_on(client: TestClient, bigeye: FakeBigeye) -> None:
    bigeye.respond(body=decision_response("REQUIRE_CONFIRMATION", [asset(FLAGGED_COLUMN, "REQUIRE_CONFIRMATION")]))
    r = post(client, gateway_body({"column": FLAGGED_COLUMN}, config={**CONFIG, "block_on": ["DENY"]}))
    assert r.status_code == 200
    assert r.json()["verdict"] is True
    assert r.json()["bigeye"]["overall_decision"] == "REQUIRE_CONFIRMATION"


def test_block_on_accepts_full_enum_names(client: TestClient, bigeye: FakeBigeye) -> None:
    bigeye.respond(body=decision_response("WARN", [asset(FLAGGED_COLUMN, "WARN")]))
    r = post(client, gateway_body({"column": FLAGGED_COLUMN}, config={**CONFIG, "block_on": ["ACCESS_DECISION_WARN"]}))
    assert r.json()["verdict"] is False


def test_only_unresolved_objects_are_allowed(client: TestClient, bigeye: FakeBigeye) -> None:
    unresolved = [{"type": "ACCESS_OBJECT_TYPE_TABLE", "externalId": "SALES.ORDERS"}]
    bigeye.respond(body=decision_response(None, [], unresolved))
    r = post(client, gateway_body({"table": "SALES.ORDERS"}))
    assert r.status_code == 200
    assert r.json()["verdict"] is True
    assert r.json()["bigeye"]["overall_decision"] == "UNSPECIFIED"
    assert r.json()["bigeye"]["unresolved_count"] == 1


@pytest.mark.parametrize(
    ("asset_decisions", "overall", "verdict"),
    [
        (["ALLOW", "DENY"], "DENY", False),
        (["ALLOW", "REQUIRE_CONFIRMATION", "WARN"], "REQUIRE_CONFIRMATION", False),
        (["ALLOW", "WARN"], "WARN", True),
        (["ALLOW", None], "ALLOW", True),
        (["WARN", "UNSPECIFIED"], "WARN", True),
        (["DENY", None], "DENY", False),
        ([None, "UNSPECIFIED"], "UNSPECIFIED", True),
        ([], "UNSPECIFIED", True),
    ],
    ids=["deny-wins", "confirmation-wins", "warn-wins", "allow-ignores-missing", "warn-ignores-unspecified",
         "deny-ignores-missing", "none-decided", "no-assets"],
)
def test_missing_overall_decision_uses_most_severe_asset(
    client: TestClient, bigeye: FakeBigeye, asset_decisions: list, overall: str, verdict: bool
) -> None:
    columns = [f"DB.S.T.C{i}" for i in range(len(asset_decisions))]
    bigeye.respond(body=decision_response(None, [asset(c, d) for c, d in zip(columns, asset_decisions, strict=True)]))
    r = post(client, gateway_body({"columns": columns} if columns else {"table": CLEAN_TABLE}))
    assert r.status_code == 200
    assert r.json()["verdict"] is verdict
    assert r.json()["bigeye"]["overall_decision"] == overall


def test_empty_response_object_is_unspecified(client: TestClient, bigeye: FakeBigeye) -> None:
    bigeye.respond(body={})
    r = post(client, gateway_body({"table": CLEAN_TABLE}))
    assert r.status_code == 200
    assert r.json()["verdict"] is True
    assert r.json()["bigeye"]["overall_decision"] == "UNSPECIFIED"
    assert r.json()["bigeye"]["assets"] == []


def test_missing_asset_decision_is_unspecified(client: TestClient, bigeye: FakeBigeye) -> None:
    bigeye.respond(body=decision_response("ALLOW", [asset(FLAGGED_COLUMN, None), asset(CLEAN_TABLE, "ALLOW")]))
    r = post(client, gateway_body({"column": FLAGGED_COLUMN, "table": CLEAN_TABLE}))
    assert r.json()["bigeye"]["overall_decision"] == "ALLOW"
    assert r.json()["bigeye"]["assets"] == [
        {"name": FLAGGED_COLUMN, "decision": "UNSPECIFIED"},
        {"name": CLEAN_TABLE, "decision": "ALLOW"},
    ]


UNRESOLVED_COLUMN = {"type": "ACCESS_OBJECT_TYPE_COLUMN", "externalId": FLAGGED_COLUMN}


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            decision_response("UNSPECIFIED", [asset(FLAGGED_COLUMN, "UNSPECIFIED")]),
            f"Bigeye UNSPECIFIED: {FLAGGED_COLUMN}",
        ),
        (decision_response(None, [asset(FLAGGED_COLUMN, None)]), f"Bigeye UNSPECIFIED: {FLAGGED_COLUMN}"),
        (decision_response(None, [], [UNRESOLVED_COLUMN]), f"Bigeye UNSPECIFIED: {FLAGGED_COLUMN} (unresolved)"),
        ({}, "Bigeye UNSPECIFIED"),
        (
            decision_response("ALLOW", [asset(CLEAN_TABLE, "ALLOW", "ACCESS_OBJECT_TYPE_TABLE"), asset(FLAGGED_COLUMN, None)]),
            f"Bigeye UNSPECIFIED: {FLAGGED_COLUMN}",
        ),
        (
            decision_response("WARN", [asset(CLEAN_TABLE, "WARN", "ACCESS_OBJECT_TYPE_TABLE")], [UNRESOLVED_COLUMN]),
            f"Bigeye UNSPECIFIED: {FLAGGED_COLUMN} (unresolved)",
        ),
        (
            decision_response(None, [asset(CLEAN_TABLE, "ALLOW", "ACCESS_OBJECT_TYPE_TABLE"), asset(FLAGGED_COLUMN, None)]),
            f"Bigeye UNSPECIFIED: {FLAGGED_COLUMN}",
        ),
    ],
    ids=[
        "overall-unspecified",
        "nothing-decided",
        "unresolved-only",
        "empty-object",
        "allow-overall-with-undecided-asset",
        "warn-overall-with-unresolved-object",
        "missing-overall-allow-with-undecided-asset",
    ],
)
@pytest.mark.parametrize("entry", ["UNSPECIFIED", "ACCESS_DECISION_UNSPECIFIED"])
def test_block_on_unspecified_blocks_any_undecided_or_unresolved_asset(
    client: TestClient, bigeye: FakeBigeye, response: dict, message: str, entry: str
) -> None:
    bigeye.respond(body=response)
    r = post(client, gateway_body({"column": FLAGGED_COLUMN}, config={**CONFIG, "block_on": ["DENY", entry]}))
    assert r.status_code == 200
    assert r.json()["verdict"] is False
    assert r.json()["message"] == message


def test_block_on_unspecified_allows_when_every_asset_is_decided(client: TestClient, bigeye: FakeBigeye) -> None:
    bigeye.respond(
        body=decision_response(
            "WARN", [asset(CLEAN_TABLE, "ALLOW", "ACCESS_OBJECT_TYPE_TABLE"), asset(FLAGGED_COLUMN, "WARN")]
        )
    )
    r = post(client, gateway_body({"column": FLAGGED_COLUMN}, config={**CONFIG, "block_on": ["DENY", "UNSPECIFIED"]}))
    assert r.status_code == 200
    assert r.json()["verdict"] is True


def test_overall_decision_in_block_on_takes_precedence_over_unspecified(client: TestClient, bigeye: FakeBigeye) -> None:
    bigeye.respond(body=decision_response("DENY", [asset(FLAGGED_COLUMN, "DENY", sensitivities=1)], [UNRESOLVED_COLUMN]))
    r = post(client, gateway_body({"column": FLAGGED_COLUMN}, config={**CONFIG, "block_on": ["DENY", "UNSPECIFIED"]}))
    assert r.json()["message"] == f"Bigeye DENY: {FLAGGED_COLUMN} (sensitive findings: 1)"


def test_default_block_on_allows_undecided_and_unresolved_assets(client: TestClient, bigeye: FakeBigeye) -> None:
    bigeye.respond(body=decision_response("ALLOW", [asset(FLAGGED_COLUMN, None)], [UNRESOLVED_COLUMN]))
    r = post(client, gateway_body({"column": FLAGGED_COLUMN}))
    assert r.json()["verdict"] is True


@pytest.mark.parametrize(
    ("response", "found"),
    [
        (decision_response("UNSPECIFIED", []), 0),
        (decision_response("ALLOW", [asset(FLAGGED_COLUMN, "ALLOW")]), 1),
        (decision_response(None, [], [UNRESOLVED_COLUMN]), 1),
        (decision_response("DENY", [asset(FLAGGED_COLUMN, "DENY"), asset(CLEAN_TABLE, "ALLOW")], [UNRESOLVED_COLUMN]), 3),
        ({}, 0),
    ],
    ids=["sql-nothing-found", "one-decision", "one-unresolved", "decisions-plus-unresolved", "empty-object"],
)
def test_assets_found_counts_decisions_and_unresolved_objects(
    client: TestClient, bigeye: FakeBigeye, response: dict, found: int
) -> None:
    bigeye.respond(body=response)
    r = post(client, gateway_body({"query": "SELECT email FROM DEMO_DB.SALES.CUSTOMERS"}))
    assert r.json()["bigeye"]["assets_found"] == found
    assert r.json()["bigeye"]["unresolved_count"] + len(r.json()["bigeye"]["assets"]) == found


NOTHING_TO_CHECK_REASON = "no SQL, tables or columns in arguments"


@pytest.mark.parametrize(
    "arguments", [{}, {"limit": 10}, {"query": "   "}, {"statement": ""}, {"tables": []}, {"columns": ""}]
)
@pytest.mark.parametrize("setting", [None, "allow", " Allow "], ids=["default", "allow", "allow-mixed-case"])
def test_nothing_to_check_skips_bigeye_with_a_warning(
    client: TestClient, bigeye: FakeBigeye, caplog: pytest.LogCaptureFixture, arguments: dict, setting: Optional[str]
) -> None:
    caplog.set_level(logging.INFO)
    config = CONFIG if setting is None else {**CONFIG, "on_nothing_to_check": setting}
    r = post(client, gateway_body(arguments, config=config))
    assert r.status_code == 200
    assert r.json() == {"verdict": True, "message": None, "bigeye": {"checked": False, "reason": NOTHING_TO_CHECK_REASON}}
    assert bigeye.requests == []
    skipped = [rec for rec in caplog.records if rec.getMessage().startswith("bigeye skipped")]
    assert [rec.levelno for rec in skipped] == [logging.WARNING]


@pytest.mark.parametrize("setting", ["block", " BLOCK "])
def test_on_nothing_to_check_block_denies_without_calling_bigeye(
    client: TestClient, bigeye: FakeBigeye, setting: str
) -> None:
    r = post(client, gateway_body({"limit": 10}, config={**CONFIG, "on_nothing_to_check": setting}))
    assert r.status_code == 200
    assert r.json() == {
        "verdict": False,
        "message": f"Bigeye could not check this call: {NOTHING_TO_CHECK_REASON}",
        "bigeye": {"checked": False, "reason": NOTHING_TO_CHECK_REASON},
    }
    assert bigeye.requests == []


def test_on_nothing_to_check_block_still_checks_calls_that_name_data(client: TestClient, bigeye: FakeBigeye) -> None:
    r = post(client, gateway_body({"table": CLEAN_TABLE}, config={**CONFIG, "on_nothing_to_check": "block"}))
    assert r.json()["verdict"] is True
    assert r.json()["bigeye"]["checked"] is True
    assert len(bigeye.requests) == 1


# --- argument names ---------------------------------------------------------

SQL = "ACCESS_OBJECT_TYPE_SQL"
TABLE = "ACCESS_OBJECT_TYPE_TABLE"
COLUMN = "ACCESS_OBJECT_TYPE_COLUMN"
CUSTOM_KEYS = {"sql_keys": ["sql_text"], "table_keys": ["target_table"], "column_keys": ["fields"]}


def sent_objects(bigeye: FakeBigeye) -> list:
    return [(o["type"], o["externalId"]) for o in bigeye.last_body()["accessObjects"]]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"query": "SELECT 1"}, [(SQL, "SELECT 1")]),
        ({"sql": "SELECT 1"}, [(SQL, "SELECT 1")]),
        ({"statement": "SELECT 1"}, [(SQL, "SELECT 1")]),
        ({"query": "SELECT 1", "statement": "SELECT 2"}, [(SQL, "SELECT 1")]),
        ({"query": "  ", "statement": "SELECT 2"}, [(SQL, "SELECT 2")]),
        ({"tables": [CLEAN_TABLE]}, [(TABLE, CLEAN_TABLE)]),
        ({"table": CLEAN_TABLE}, [(TABLE, CLEAN_TABLE)]),
        ({"columns": [FLAGGED_COLUMN]}, [(COLUMN, FLAGGED_COLUMN)]),
        ({"column": FLAGGED_COLUMN}, [(COLUMN, FLAGGED_COLUMN)]),
    ],
    ids=["query", "sql", "statement", "first-sql-key-wins", "blank-sql-key-skipped", "tables", "table", "columns",
         "column"],
)
def test_default_argument_names(client: TestClient, bigeye: FakeBigeye, arguments: dict, expected: list) -> None:
    r = post(client, gateway_body(arguments))
    assert r.json()["bigeye"]["checked"] is True
    assert sent_objects(bigeye) == expected


def test_configured_argument_names_are_read(client: TestClient, bigeye: FakeBigeye) -> None:
    arguments = {"sql_text": "SELECT 1", "target_table": CLEAN_TABLE, "fields": [FLAGGED_COLUMN]}
    r = post(client, gateway_body(arguments, config={**CONFIG, **CUSTOM_KEYS}))
    assert r.json()["bigeye"]["checked"] is True
    assert sent_objects(bigeye) == [(TABLE, CLEAN_TABLE), (COLUMN, FLAGGED_COLUMN), (SQL, "SELECT 1")]


@pytest.mark.parametrize(
    "arguments", [{"query": "SELECT 1"}, {"statement": "SELECT 1"}, {"table": CLEAN_TABLE}, {"columns": [FLAGGED_COLUMN]}]
)
def test_configured_argument_names_replace_the_defaults(client: TestClient, bigeye: FakeBigeye, arguments: dict) -> None:
    r = post(client, gateway_body(arguments, config={**CONFIG, **CUSTOM_KEYS}))
    assert r.json()["bigeye"] == {"checked": False, "reason": NOTHING_TO_CHECK_REASON}
    assert bigeye.requests == []


def test_unconfigured_argument_names_keep_their_defaults(client: TestClient, bigeye: FakeBigeye) -> None:
    config = {**CONFIG, "sql_keys": [" sql_text ", "sql_text"]}
    post(client, gateway_body({"sql_text": "SELECT 1", "query": "SELECT 2", "table": CLEAN_TABLE}, config=config))
    assert sent_objects(bigeye) == [(TABLE, CLEAN_TABLE), (SQL, "SELECT 1")]


def test_nested_arguments_are_not_read(client: TestClient, bigeye: FakeBigeye) -> None:
    r = post(client, gateway_body({"params": {"query": "SELECT 1", "table": CLEAN_TABLE}}))
    assert r.json()["bigeye"] == {"checked": False, "reason": NOTHING_TO_CHECK_REASON}
    assert bigeye.requests == []


def test_sql_text_is_never_echoed(client: TestClient, bigeye: FakeBigeye) -> None:
    bigeye.respond(
        body=decision_response(
            "DENY",
            [
                asset(SECRET_SQL, "DENY", "ACCESS_OBJECT_TYPE_SQL"),
                asset(FLAGGED_COLUMN, "DENY", sensitivities=1),
            ],
        )
    )
    r = post(client, gateway_body({"query": SECRET_SQL}))
    assert r.json()["verdict"] is False
    assert "alice@example.com" not in r.text
    assert "SELECT" not in r.text
    assert r.json()["bigeye"]["assets"][0] == {"name": "<sql>", "decision": "DENY"}


# --- errors -----------------------------------------------------------------


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


@pytest.mark.parametrize(
    ("setup", "status"),
    [
        (lambda b: b.respond(401, {"message": "unauthorized"}), 502),
        (lambda b: b.respond(403, {"message": "forbidden"}), 502),
        (lambda b: b.respond(500, {"message": "boom"}), 502),
        (lambda b: b.respond(302, raw=b""), 502),
        (lambda b: setattr(b, "reply", _timeout), 504),
        (lambda b: b.respond(200, raw=b"<html>not json</html>"), 502),
        (lambda b: b.respond(200, body=["not", "an", "object"]), 502),
        (lambda b: b.respond(200, body={"overallDecision": "ACCESS_DECISION_MAYBE"}), 502),
        (lambda b: b.respond(200, body=decision_response(None, [asset(FLAGGED_COLUMN, "MAYBE")])), 502),
    ],
    ids=["401", "403", "500", "redirect", "timeout", "not-json", "not-object", "unknown-overall-decision",
         "unknown-asset-decision-without-overall"],
)
def test_bigeye_failures_map_to_502_or_504(client: TestClient, bigeye: FakeBigeye, setup, status: int) -> None:
    setup(bigeye)
    r = post(client, gateway_body({"column": FLAGGED_COLUMN}))
    assert r.status_code == status
    assert r.status_code not in (429, 503)
    assert API_KEY not in r.text
    assert len(bigeye.requests) == 1


def test_rejected_key_is_logged_without_the_key(
    client: TestClient, bigeye: FakeBigeye, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    bigeye.respond(401, {"message": "unauthorized"})
    post(client, gateway_body({"column": FLAGGED_COLUMN}))
    assert "Bigeye rejected the API key (check auth_scheme and workspace access)" in caplog.text
    assert API_KEY not in caplog.text


KEY_DECODE_HINT = "Bigeye could not decode the API key"
BASE64_JSON_ERROR = {"message": "ServletException: java.lang.IllegalArgumentException: Illegal base64 character 5f"}
BASE64_HTML_ERROR = b"<html><title>Error 500 java.lang.IllegalArgumentException: Illegal base64 character 2d</title></html>"


@pytest.mark.parametrize(
    "setup",
    [lambda b: b.respond(500, BASE64_JSON_ERROR), lambda b: b.respond(500, raw=BASE64_HTML_ERROR)],
    ids=["json-body", "html-body"],
)
def test_base64_500_logs_key_format_hint(
    client: TestClient, bigeye: FakeBigeye, caplog: pytest.LogCaptureFixture, setup
) -> None:
    caplog.set_level(logging.DEBUG)
    setup(bigeye)
    r = post(client, gateway_body({"column": FLAGGED_COLUMN}))
    assert r.status_code == 502
    assert KEY_DECODE_HINT in caplog.text
    assert "auth_scheme apikey" in caplog.text
    assert "Illegal base64" not in caplog.text
    assert API_KEY not in caplog.text
    assert len(bigeye.requests) == 1


@pytest.mark.parametrize(
    "setup",
    [lambda b: b.respond(500, {"message": "boom"}), lambda b: b.respond(502, BASE64_JSON_ERROR)],
    ids=["500-without-base64", "base64-but-not-500"],
)
def test_other_errors_do_not_log_key_format_hint(
    client: TestClient, bigeye: FakeBigeye, caplog: pytest.LogCaptureFixture, setup
) -> None:
    caplog.set_level(logging.DEBUG)
    setup(bigeye)
    r = post(client, gateway_body({"column": FLAGGED_COLUMN}))
    assert r.status_code == 502
    assert KEY_DECODE_HINT not in caplog.text


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com",
        "http://app.bigeye.com",
        "https://app.bigeye.com:8443",
        "https://bigeye.com.evil.io",
        "https://app.bigeye.com.evil.io",
        "https://evilbigeye.com",
        "https://app.bigeye.com@evil.io",
        "https://bigeye.com",
        "https://evil%2eio.bigeye.com",
        "https://.bigeye.com",
    ],
)
def test_disallowed_host_returns_500_without_calling_bigeye(
    client: TestClient, bigeye: FakeBigeye, caplog: pytest.LogCaptureFixture, url: str
) -> None:
    caplog.set_level(logging.ERROR)
    r = post(client, gateway_body({"column": FLAGGED_COLUMN}, config={**CONFIG, "bigeye_url": url}))
    assert r.status_code == 500
    assert bigeye.requests == []
    assert any(rec.levelno == logging.ERROR and "not allowed" in rec.getMessage() for rec in caplog.records)


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://acme.bigeye.com", "acme.bigeye.com"),
        ("https://app.bigeye.com/", "app.bigeye.com"),
        ("https://ACME.bigeye.com:443/ignored/path?x=1", "acme.bigeye.com"),
    ],
)
def test_default_allowlist_accepts_bigeye_subdomains(client: TestClient, bigeye: FakeBigeye, url: str, host: str) -> None:
    r = post(client, gateway_body({"column": FLAGGED_COLUMN}, config={**CONFIG, "bigeye_url": url}))
    assert r.status_code == 200
    assert str(bigeye.requests[0].url) == f"https://{host}/api/v1/access-decision"


def test_allowlist_comes_from_env(client: TestClient, bigeye: FakeBigeye, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIGEYE_ALLOWED_HOSTS", "acme.bigeye.com")
    r = post(client, gateway_body({"column": FLAGGED_COLUMN}, config={**CONFIG, "bigeye_url": "https://acme.bigeye.com"}))
    assert r.status_code == 200
    assert str(bigeye.requests[0].url) == "https://acme.bigeye.com/api/v1/access-decision"
    r = post(client, gateway_body({"column": FLAGGED_COLUMN}))
    assert r.status_code == 500


@pytest.mark.parametrize(
    "override",
    [
        {"workspace_id": None},
        {"workspace_id": "abc"},
        {"auth_scheme": "basic"},
        {"block_on": ["NOPE"]},
        {"block_on": "DENY"},
        {"bigeye_timeout_ms": 10},
        {"evaluate_policy": "maybe"},
        {"warehouse_type": "snowflakes"},
        {"warehouse_type": "unspecified"},
        {"warehouse_type": "DATABASE_TYPE_"},
        {"sql_keys": "statement"},
        {"sql_keys": [""]},
        {"table_keys": [7]},
        {"column_keys": {"name": "column"}},
        {"on_nothing_to_check": "deny"},
        {"on_nothing_to_check": True},
    ],
)
def test_bad_config_returns_500(client: TestClient, bigeye: FakeBigeye, override: dict) -> None:
    r = post(client, gateway_body({"column": FLAGGED_COLUMN}, config={**CONFIG, **override}))
    assert r.status_code == 500
    assert r.json()["error"] == "configuration_error"
    assert bigeye.requests == []


def test_sql_without_source_name_returns_500(client: TestClient, bigeye: FakeBigeye) -> None:
    config = {k: v for k, v in CONFIG.items() if k != "source_name"}
    r = post(client, gateway_body({"query": "SELECT 1"}, config=config))
    assert r.status_code == 500
    assert bigeye.requests == []


# --- config location and response casing ------------------------------------


@pytest.mark.parametrize("nested", [True, False], ids=["config.config", "top-level"])
def test_config_is_read_from_either_location(client: TestClient, bigeye: FakeBigeye, nested: bool) -> None:
    config = {**CONFIG, "bigeye_url": "https://app.bigeye.com", "workspace_id": 777, "operation": "custom_op"}
    r = post(client, gateway_body({"table": CLEAN_TABLE}, config=config, nested=nested))
    assert r.status_code == 200
    assert str(bigeye.requests[0].url) == "https://app.bigeye.com/api/v1/access-decision"
    sent = bigeye.last_body()
    assert sent["workspace"] == 777
    assert sent["operation"] == "custom_op"


@pytest.mark.parametrize("snake", [False, True], ids=["camelCase", "snake_case"])
def test_response_casing(client: TestClient, bigeye: FakeBigeye, snake: bool) -> None:
    response = decision_response("DENY", [asset(FLAGGED_COLUMN, "DENY", sensitivities=2, open_issues=1)], [{"type": 1}])
    bigeye.respond(body=to_snake_case(response) if snake else response)
    r = post(client, gateway_body({"column": FLAGGED_COLUMN}))
    body = r.json()
    assert body["verdict"] is False
    assert body["message"] == f"Bigeye DENY: {FLAGGED_COLUMN} (sensitive findings: 2, open issues: 1)"
    assert body["bigeye"]["assets"] == [{"name": FLAGGED_COLUMN, "decision": "DENY"}]
    assert body["bigeye"]["unresolved_count"] == 1


# --- request sent to Bigeye -------------------------------------------------


@pytest.mark.parametrize(("scheme", "prefix"), [("apikey", "apikey"), ("bearer", "Bearer"), ("Bearer", "Bearer")])
def test_key_is_forwarded_with_auth_scheme(client: TestClient, bigeye: FakeBigeye, scheme: str, prefix: str) -> None:
    post(client, gateway_body({"table": CLEAN_TABLE}, config={**CONFIG, "auth_scheme": scheme}))
    assert bigeye.requests[0].headers["authorization"] == f"{prefix} {API_KEY}"


def test_request_body_for_a_user(client: TestClient, bigeye: FakeBigeye) -> None:
    arguments = {"query": "SELECT 1", "tables": [CLEAN_TABLE, CLEAN_TABLE], "columns": f"{FLAGGED_COLUMN}, X.Y.Z.W"}
    r = post(client, gateway_body(arguments))
    sent = bigeye.last_body()
    common = {"workspaceId": 355, "sourceName": "demo-snowflake", "sourceType": "DATABASE_TYPE_SNOWFLAKE"}
    assert sent["accessObjects"] == [
        {"type": "ACCESS_OBJECT_TYPE_TABLE", "externalId": CLEAN_TABLE, **common},
        {"type": "ACCESS_OBJECT_TYPE_COLUMN", "externalId": FLAGGED_COLUMN, **common},
        {"type": "ACCESS_OBJECT_TYPE_COLUMN", "externalId": "X.Y.Z.W", **common},
        {"type": "ACCESS_OBJECT_TYPE_SQL", "externalId": "SELECT 1", **common},
    ]
    assert sent["workspace"] == 355
    assert sent["agentExternalId"] == "truefoundry-gateway"
    assert sent["aiAgent"] == {"name": "truefoundry-gateway", "platform": "AI_PLATFORM_TRUEFOUNDRY"}
    assert sent["username"] == "alice@example.com"
    assert sent["operation"] == "mcp_tool_call"
    assert sent["evaluatePolicy"] is False
    assert sent["spanExternalId"] == r.json()["bigeye"]["span_external_id"]
    assert "conversationExternalId" not in sent


def test_request_body_for_a_virtual_account(client: TestClient, bigeye: FakeBigeye) -> None:
    user = {"subjectId": "va-1", "subjectType": "virtualaccount", "subjectSlug": "analytics-bot"}
    post(client, gateway_body({"table": CLEAN_TABLE}, user=user))
    sent = bigeye.last_body()
    assert sent["agentExternalId"] == "analytics-bot"
    assert sent["aiAgent"]["name"] == "analytics-bot"
    assert "username" not in sent


@pytest.mark.parametrize(
    ("configured", "sent"),
    [
        ("DATABASE_TYPE_SNOWFLAKE", "DATABASE_TYPE_SNOWFLAKE"),
        ("WAREHOUSE_TYPE_SNOWFLAKE", "DATABASE_TYPE_SNOWFLAKE"),
        (" warehouse_type_bigquery ", "DATABASE_TYPE_BIGQUERY"),
        ("database_type_redshift", "DATABASE_TYPE_REDSHIFT"),
        ("snowflake", "DATABASE_TYPE_SNOWFLAKE"),
        ("Sql_Server", "DATABASE_TYPE_SQL_SERVER"),
        (" databricks ", "DATABASE_TYPE_DATABRICKS"),
    ],
)
def test_warehouse_type_is_sent_as_database_type(
    client: TestClient, bigeye: FakeBigeye, configured: str, sent: str
) -> None:
    post(client, gateway_body({"table": CLEAN_TABLE}, config={**CONFIG, "warehouse_type": configured}))
    assert bigeye.last_body()["accessObjects"][0]["sourceType"] == sent


@pytest.mark.parametrize("unset", [None, "", "  ", "absent"])
def test_source_type_is_omitted_when_warehouse_type_is_unset(
    client: TestClient, bigeye: FakeBigeye, unset: Optional[str]
) -> None:
    config = {k: v for k, v in CONFIG.items() if k != "warehouse_type"}
    if unset != "absent":
        config["warehouse_type"] = unset
    post(client, gateway_body({"query": "SELECT 1", "table": CLEAN_TABLE}, config=config))
    for access_object in bigeye.last_body()["accessObjects"]:
        assert "sourceType" not in access_object
        assert access_object["sourceName"] == "demo-snowflake"


def test_span_id_is_new_per_call(client: TestClient, bigeye: FakeBigeye) -> None:
    post(client, gateway_body({"table": CLEAN_TABLE}))
    post(client, gateway_body({"table": CLEAN_TABLE}))
    first, second = (json.loads(r.content)["spanExternalId"] for r in bigeye.requests)
    assert first != second


@pytest.mark.parametrize(
    ("arguments", "tables"),
    [
        ({"table": CLEAN_TABLE}, [CLEAN_TABLE]),
        ({"tables": f'["{CLEAN_TABLE}", "A.B.C"]'}, [CLEAN_TABLE, "A.B.C"]),
        ({"tables": f"{CLEAN_TABLE},A.B.C"}, [CLEAN_TABLE, "A.B.C"]),
        ({"tables": [CLEAN_TABLE], "table": "A.B.C"}, [CLEAN_TABLE, "A.B.C"]),
        ({"tables": ["SALES.ORDERS", 7, None]}, ["SALES.ORDERS"]),
    ],
)
def test_table_extraction(client: TestClient, bigeye: FakeBigeye, arguments: dict, tables: list) -> None:
    post(client, gateway_body(arguments))
    assert [o["externalId"] for o in bigeye.last_body()["accessObjects"]] == tables


# --- payload logging --------------------------------------------------------


def test_payload_logging_is_off_by_default(
    client: TestClient, bigeye: FakeBigeye, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    post(client, gateway_body({"table": CLEAN_TABLE}))
    assert "incoming payload" not in caplog.text


def test_payload_logging_redacts_identity(
    client: TestClient, bigeye: FakeBigeye, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_PAYLOADS", "true")
    caplog.set_level(logging.INFO)
    body = gateway_body({"table": CLEAN_TABLE, "note": "x" * 900})
    body["config"]["headers"] = {"X-Extra": "header-secret"}
    body["context"]["claims"] = {"email": "alice@example.com", "groups": ["eng"]}
    post(client, body)

    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("incoming payload: "))
    logged = json.loads(line.removeprefix("incoming payload: "))
    assert logged["context"]["user"] == {
        "subjectId": "<redacted:str>",
        "subjectType": "user",
        "subjectSlug": "<redacted:str>",
        "tenantName": "<redacted:str>",
    }
    assert logged["context"]["claims"] == {"email": "<redacted:str>", "groups": ["<redacted:str>"]}
    assert logged["context"]["metadata"] == {"tfy_trace_id": "<redacted:str>"}
    assert logged["config"]["headers"] == {"X-Extra": "<redacted:str>"}
    assert logged["config"]["config"]["workspace_id"] == 355
    assert logged["requestBody"]["table"] == CLEAN_TABLE
    assert logged["requestBody"]["note"].endswith("...<truncated 900 chars>")
    for secret in ("alice@example.com", "header-secret", "u-123", API_KEY):
        assert secret not in caplog.text


def test_bigeye_error_message_is_logged_only_with_log_payloads(
    client: TestClient, bigeye: FakeBigeye, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.INFO)
    bigeye.respond(400, {"message": "workspace mismatch"})
    post(client, gateway_body({"column": FLAGGED_COLUMN}))
    assert "workspace mismatch" not in caplog.text

    monkeypatch.setenv("LOG_PAYLOADS", "true")
    caplog.clear()
    post(client, gateway_body({"column": FLAGGED_COLUMN}))
    assert "workspace mismatch" in caplog.text


# --- retry safety -----------------------------------------------------------
# The gateway retries a custom guardrail only on 429 and 503, honouring
# Retry-After (tfy-llm-gateway src/plugins/utils.ts). Passing either through
# would multiply Bigeye calls, so every Bigeye failure must become 502 or 504.


def _connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


@pytest.mark.parametrize("status", [400, 404, 408, 409, 422, 429, 500, 502, 503, 504])
def test_no_bigeye_status_reaches_the_gateway_as_retryable(client: TestClient, bigeye: FakeBigeye, status: int) -> None:
    bigeye.reply = lambda _: httpx.Response(status, headers={"Retry-After": "1"}, json={"message": "nope"})
    r = post(client, gateway_body({"column": FLAGGED_COLUMN}))
    assert r.status_code == 502
    assert "retry-after" not in r.headers
    assert len(bigeye.requests) == 1


def test_connection_error_is_502_after_one_attempt(client: TestClient, bigeye: FakeBigeye) -> None:
    bigeye.reply = _connect_error
    r = post(client, gateway_body({"column": FLAGGED_COLUMN}))
    assert r.status_code == 502
    assert len(bigeye.requests) == 1


def test_configured_timeout_is_applied_to_the_bigeye_call(client: TestClient, bigeye: FakeBigeye) -> None:
    post(client, gateway_body({"column": FLAGGED_COLUMN}, config={**CONFIG, "bigeye_timeout_ms": 1234}))
    assert bigeye.requests[0].extensions["timeout"] == {"connect": 1.234, "read": 1.234, "write": 1.234, "pool": 1.234}


# --- no sensitive data leaks, on every path ---------------------------------

SQL_LITERAL = "sql-literal-secret"
LEAK_SQL = f"SELECT EMAIL FROM DEMO_DB.SALES.CUSTOMERS WHERE NOTE = '{SQL_LITERAL}'"
SLUG = "alice@example.com"
IDENTITY_SECRETS = ("subj-secret-123", "tenant-secret-xyz", "claims-secret@example.com", "meta-secret-456", "header-secret-789")
INCOMING_HEADER = ("X-Tfy-Internal", "incoming-header-secret")
NEVER_SENT_TO_BIGEYE = (API_KEY, INCOMING_HEADER[1], *IDENTITY_SECRETS)
NEVER_RETURNED_OR_LOGGED = (*NEVER_SENT_TO_BIGEYE, SLUG, SQL_LITERAL)
BIGEYE_BODY_KEYS = {
    "accessObjects", "workspace", "agentExternalId", "aiAgent", "operation", "spanExternalId", "evaluatePolicy", "username",
}
BIGEYE_HEADER_NAMES = {
    "host", "accept", "accept-encoding", "connection", "user-agent", "content-length", "content-type", "authorization",
}


def leak_probe_body(arguments: Optional[dict] = None, config: Optional[dict] = None) -> dict:
    user = {"subjectId": "subj-secret-123", "subjectType": "user", "subjectSlug": SLUG, "tenantName": "tenant-secret-xyz"}
    body = gateway_body({"query": LEAK_SQL, "column": FLAGGED_COLUMN} if arguments is None else arguments, config, user=user)
    body["config"]["headers"] = {"X-Extra": "header-secret-789"}
    body["context"]["claims"] = {"email": "claims-secret@example.com"}
    body["context"]["metadata"] = {"tfy_trace_id": "meta-secret-456"}
    return body


def _reply(status: int = 200, body=None, raw: Optional[bytes] = None) -> Callable[[FakeBigeye], None]:
    return lambda fake: fake.respond(status, body, raw)


LEAK_SCENARIOS = [
    pytest.param(_reply(body=decision_response("ALLOW", [asset(FLAGGED_COLUMN, "ALLOW")])), {}, 200, id="allow"),
    pytest.param(
        _reply(
            body=decision_response(
                "DENY", [asset(LEAK_SQL, "DENY", "ACCESS_OBJECT_TYPE_SQL"), asset(FLAGGED_COLUMN, "DENY")]
            )
        ),
        {},
        200,
        id="deny",
    ),
    pytest.param(
        _reply(body=decision_response("REQUIRE_CONFIRMATION", [asset(FLAGGED_COLUMN, "REQUIRE_CONFIRMATION")])),
        {},
        200,
        id="require-confirmation",
    ),
    pytest.param(
        _reply(body=decision_response(None, [], [{"type": "ACCESS_OBJECT_TYPE_SQL", "externalId": LEAK_SQL}])),
        {},
        200,
        id="unresolved-only",
    ),
    pytest.param(None, {"arguments": {"limit": 5}}, 200, id="nothing-to-check"),
    pytest.param(
        None,
        {"arguments": {"limit": 5}, "config": {**CONFIG, "on_nothing_to_check": "block"}},
        200,
        id="nothing-to-check-block",
    ),
    pytest.param(_reply(401, {"message": f"bad key near {SQL_LITERAL}"}), {}, 502, id="bigeye-401"),
    pytest.param(_reply(400, {"message": f"cannot parse {LEAK_SQL}"}), {}, 502, id="bigeye-400-quotes-sql"),
    pytest.param(_reply(429, {"message": "slow down"}), {}, 502, id="bigeye-429"),
    pytest.param(_reply(503, {"message": "unavailable"}), {}, 502, id="bigeye-503"),
    pytest.param(lambda fake: setattr(fake, "reply", _timeout), {}, 504, id="timeout"),
    pytest.param(lambda fake: setattr(fake, "reply", _connect_error), {}, 502, id="connect-error"),
    pytest.param(_reply(raw=b"<html>oops</html>"), {}, 502, id="not-json"),
    pytest.param(
        _reply(body=decision_response(None, [asset(LEAK_SQL, None, "ACCESS_OBJECT_TYPE_SQL"), asset(FLAGGED_COLUMN, "DENY")])),
        {},
        200,
        id="missing-decisions",
    ),
    pytest.param(_reply(body=decision_response(None, [asset(FLAGGED_COLUMN, "MAYBE")])), {}, 502, id="unknown-decision"),
    pytest.param(
        _reply(body=decision_response("ALLOW", [], [{"type": "ACCESS_OBJECT_TYPE_SQL", "externalId": LEAK_SQL}])),
        {"config": {**CONFIG, "block_on": ["DENY", "UNSPECIFIED"]}},
        200,
        id="unspecified-blocks-unresolved-sql",
    ),
    pytest.param(_reply(500, {"message": f"Illegal base64 near {SQL_LITERAL}"}), {}, 502, id="bigeye-500-base64"),
    pytest.param(None, {"config": {**CONFIG, "bigeye_url": "https://evil.example.com"}}, 500, id="disallowed-host"),
    pytest.param(None, {"config": {**CONFIG, "workspace_id": None}}, 500, id="bad-config"),
]


def _send_leak_probe(client: TestClient, bigeye: FakeBigeye, setup, overrides: dict) -> httpx.Response:
    if setup:
        setup(bigeye)
    headers = {**AUTH, INCOMING_HEADER[0]: INCOMING_HEADER[1]}
    return client.post("/mcp-pre-invoke", json=leak_probe_body(**overrides), headers=headers)


@pytest.mark.parametrize(("setup", "overrides", "status"), LEAK_SCENARIOS)
def test_no_secrets_in_responses_logs_or_bigeye_calls(
    client: TestClient, bigeye: FakeBigeye, caplog: pytest.LogCaptureFixture, setup, overrides: dict, status: int
) -> None:
    caplog.set_level(logging.DEBUG)
    r = _send_leak_probe(client, bigeye, setup, overrides)
    assert r.status_code == status

    returned = r.text + json.dumps(dict(r.headers))
    for secret in NEVER_RETURNED_OR_LOGGED:
        assert secret not in returned, f"{secret!r} returned to the gateway"
        assert secret not in caplog.text, f"{secret!r} logged"

    for request in bigeye.requests:
        sent_body = request.content.decode()
        assert set(json.loads(sent_body)) <= BIGEYE_BODY_KEYS
        assert set(request.headers.keys()) <= BIGEYE_HEADER_NAMES
        assert request.headers["authorization"] == f"apikey {API_KEY}"
        other_headers = json.dumps({k: v for k, v in request.headers.items() if k != "authorization"})
        for secret in NEVER_SENT_TO_BIGEYE:
            assert secret not in sent_body, f"{secret!r} sent to Bigeye in the body"
        for secret in NEVER_RETURNED_OR_LOGGED:
            assert secret not in other_headers, f"{secret!r} sent to Bigeye in a header"


@pytest.mark.parametrize(("setup", "overrides", "status"), LEAK_SCENARIOS)
def test_log_payloads_mode_still_hides_key_and_identity(
    client: TestClient,
    bigeye: FakeBigeye,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    setup,
    overrides: dict,
    status: int,
) -> None:
    monkeypatch.setenv("LOG_PAYLOADS", "true")
    caplog.set_level(logging.DEBUG)
    r = _send_leak_probe(client, bigeye, setup, overrides)
    assert r.status_code == status
    assert "incoming payload: " in caplog.text
    for secret in (*NEVER_SENT_TO_BIGEYE, SLUG):
        assert secret not in caplog.text, f"{secret!r} logged with LOG_PAYLOADS=true"


@pytest.mark.parametrize(
    ("field", "value"),
    [("requestBody", f"{LEAK_SQL} subj-secret-123"), ("context", {"user": f"{SLUG} tenant-secret-xyz"})],
)
def test_invalid_payload_is_not_echoed(
    client: TestClient, bigeye: FakeBigeye, caplog: pytest.LogCaptureFixture, field: str, value
) -> None:
    caplog.set_level(logging.DEBUG)
    body = leak_probe_body()
    body[field] = value
    r = post(client, body)
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_payload"
    assert r.json()["detail"][0]["loc"][:2] == ["body", field]
    for secret in NEVER_RETURNED_OR_LOGGED:
        assert secret not in r.text
        assert secret not in caplog.text
    assert bigeye.requests == []


@pytest.fixture
def local_bigeye(monkeypatch: pytest.MonkeyPatch) -> Iterator[list]:
    """A real HTTP server standing in for Bigeye, so the actual httpx/httpcore stack and its logging run."""
    received: list = []
    deny = json.dumps(decision_response("DENY", [asset(FLAGGED_COLUMN, "DENY", sensitivities=1)])).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("content-length", 0)))
            received.append(dict(self.headers))
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(deny)))
            self.end_headers()
            self.wfile.write(deny)

        def log_message(self, *_args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(_settings, "resolve_base_url", lambda _url: f"http://127.0.0.1:{server.server_port}")
    yield received
    server.shutdown()
    server.server_close()


def test_api_key_is_not_logged_by_the_real_http_stack_at_debug(
    client: TestClient, local_bigeye: list, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    r = post(client, leak_probe_body())
    assert r.status_code == 200
    assert r.json()["verdict"] is False
    assert local_bigeye[0]["Authorization"] == f"apikey {API_KEY}"
    assert any(rec.name.startswith("httpcore") for rec in caplog.records), "httpcore debug logging did not run"
    for secret in NEVER_RETURNED_OR_LOGGED:
        assert secret not in caplog.text, f"{secret!r} logged by the HTTP stack"
