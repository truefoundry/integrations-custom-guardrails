"""Per-request Bigeye settings, parsed from the guardrail's Config JSON.

The gateway sends the whole custom-guardrail config object as ``config``
(``{url, headers, config}``); the dashboard Config JSON is the nested
``config.config``. Direct callers such as curl may send the settings
at the top level instead, so both shapes are accepted.

Bigeye hosts are restricted to ``BIGEYE_ALLOWED_HOSTS`` because the wrapper
forwards the caller's API key to whatever ``bigeye_url`` the config names.
Entries are exact hostnames, or ``*.domain`` for any subdomain of ``domain``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

log = logging.getLogger("bigeye-guardrails-tfy")

DEFAULT_BIGEYE_URL = "https://app.bigeye.com"
DEFAULT_ALLOWED_HOSTS = "*.bigeye.com"
WILDCARD_PREFIX = "*."
# Plain DNS labels only, so a suffix match can't be satisfied by an encoded or malformed host.
HOSTNAME_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*")
DEFAULT_TIMEOUT_MS = 3500
MIN_TIMEOUT_MS = 100
MAX_TIMEOUT_MS = 30000
DEFAULT_BLOCK_ON = ("DENY", "REQUIRE_CONFIRMATION")
DEFAULT_AGENT_ID = "truefoundry-gateway"
DEFAULT_OPERATION = "mcp_tool_call"
DEFAULT_SQL_KEYS = ("query", "sql", "statement")
DEFAULT_TABLE_KEYS = ("tables", "table")
DEFAULT_COLUMN_KEYS = ("columns", "column")
ALLOW_UNCHECKED = "allow"
BLOCK_UNCHECKED = "block"
DEFAULT_ON_NOTHING_TO_CHECK = ALLOW_UNCHECKED

AUTH_SCHEMES = {"apikey": "apikey", "bearer": "Bearer"}
DECISION_PREFIX = "ACCESS_DECISION_"
DECISIONS = ("ALLOW", "WARN", "REQUIRE_CONFIRMATION", "DENY", "UNSPECIFIED")
DATABASE_TYPE_PREFIX = "DATABASE_TYPE_"
# Bigeye's doc examples use WAREHOUSE_TYPE_*, but its OpenAPI sourceType enum is DATABASE_TYPE_*.
DOC_WAREHOUSE_TYPE_PREFIX = "WAREHOUSE_TYPE_"
# generatedWarehouseType in Bigeye's access-decision OpenAPI, minus UNSPECIFIED. Plain names map onto these.
DATABASE_TYPES = frozenset(
    {
        "AWS_ATHENA", "BIGQUERY", "DATABRICKS", "MYSQL", "ORACLE", "POSTGRES", "PRESTO", "REDSHIFT", "SAP_HANA",
        "SNOWFLAKE", "SQL_SERVER", "SYNAPSE", "ROCKSET", "VERTICA", "AVATICA", "DB2", "TERADATA", "NETEZZA",
        "SALESFORCE",
    }
)


class ConfigError(Exception):
    """Guardrail misconfiguration. Surfaces as HTTP 500 so the enforcing strategy decides."""


@dataclass(frozen=True)
class BigeyeSettings:
    base_url: str
    workspace_id: int
    auth_scheme: str
    source_name: Optional[str]
    warehouse_type: Optional[str]
    block_on: frozenset
    evaluate_policy: bool
    timeout_ms: int
    agent_id: str
    operation: str
    sql_keys: tuple
    table_keys: tuple
    column_keys: tuple
    on_nothing_to_check: str


def normalize_decision(value: Any) -> Optional[str]:
    """Map ``ACCESS_DECISION_DENY`` or ``DENY`` (any case) to ``DENY``; None if unknown."""
    if not isinstance(value, str):
        return None
    short = value.strip().upper()
    if short.startswith(DECISION_PREFIX):
        short = short[len(DECISION_PREFIX):]
    return short if short in DECISIONS else None


def allowed_hosts() -> frozenset:
    raw = os.environ.get("BIGEYE_ALLOWED_HOSTS", DEFAULT_ALLOWED_HOSTS)
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def _entry_matches(host: str, entry: str) -> bool:
    """``*.domain`` matches subdomains on a dot boundary, never ``domain`` itself; other entries match exactly."""
    if entry.startswith(WILDCARD_PREFIX):
        return host.endswith("." + entry[len(WILDCARD_PREFIX):])
    return host == entry


def host_allowed(host: str, entries: frozenset) -> bool:
    return bool(HOSTNAME_PATTERN.fullmatch(host)) and any(_entry_matches(host, entry) for entry in entries)


def resolve_base_url(url: str) -> str:
    """Return ``https://<host>`` if ``url`` is https on an allowlisted host; raise otherwise."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.port not in (None, 443) or not host_allowed(host, allowed_hosts()):
        log.error(
            "bigeye_url host %r (scheme %r) is not allowed; allowed https hosts: %s",
            host,
            parts.scheme,
            ",".join(sorted(allowed_hosts())),
        )
        raise ConfigError("bigeye_url is not an allowed Bigeye host (see BIGEYE_ALLOWED_HOSTS)")
    return f"https://{host}"


def dashboard_config(raw: Optional[dict]) -> dict:
    if not raw:
        return {}
    inner = raw.get("config")
    return inner if isinstance(inner, dict) else raw


def _workspace_id(value: Any) -> int:
    if isinstance(value, bool) or value in (None, ""):
        raise ConfigError("workspace_id is required")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError("workspace_id must be an integer") from None


def _auth_scheme(value: Any) -> str:
    scheme = AUTH_SCHEMES.get(str(value or "apikey").strip().lower())
    if scheme is None:
        raise ConfigError("auth_scheme must be 'apikey' or 'bearer'")
    return scheme


def _block_on(value: Any) -> frozenset:
    if value is None:
        return frozenset(DEFAULT_BLOCK_ON)
    if not isinstance(value, list):
        raise ConfigError("block_on must be a list of decisions")
    decisions = {normalize_decision(v) for v in value}
    if None in decisions:
        raise ConfigError(f"block_on entries must be among {', '.join(DECISIONS)}")
    return frozenset(decisions)


def _bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ConfigError(f"{name} must be true or false")


def _timeout_ms(value: Any) -> int:
    if value is None:
        return DEFAULT_TIMEOUT_MS
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        raise ConfigError("bigeye_timeout_ms must be an integer") from None
    if not MIN_TIMEOUT_MS <= timeout <= MAX_TIMEOUT_MS:
        raise ConfigError(f"bigeye_timeout_ms must be between {MIN_TIMEOUT_MS} and {MAX_TIMEOUT_MS}")
    return timeout


def _optional_str(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _warehouse_type(value: Any) -> Optional[str]:
    """``DATABASE_TYPE_SNOWFLAKE`` as is; ``WAREHOUSE_TYPE_SNOWFLAKE`` or ``snowflake`` mapped to it; None if unset."""
    name = _optional_str(value)
    if name is None:
        return None
    name = name.upper()
    if name.startswith(DOC_WAREHOUSE_TYPE_PREFIX):
        name = DATABASE_TYPE_PREFIX + name[len(DOC_WAREHOUSE_TYPE_PREFIX):]
    elif name in DATABASE_TYPES:
        name = DATABASE_TYPE_PREFIX + name
    if not name.startswith(DATABASE_TYPE_PREFIX) or name == DATABASE_TYPE_PREFIX:
        raise ConfigError(
            "warehouse_type must be a Bigeye warehouse type, e.g. DATABASE_TYPE_SNOWFLAKE or snowflake"
        )
    return name


def _argument_keys(value: Any, name: str, default: tuple) -> tuple:
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(key, str) and key.strip() for key in value):
        raise ConfigError(f"{name} must be a list of top-level argument names")
    return tuple(dict.fromkeys(key.strip() for key in value))


def _on_nothing_to_check(value: Any) -> str:
    if value is None:
        return DEFAULT_ON_NOTHING_TO_CHECK
    choice = value.strip().lower() if isinstance(value, str) else None
    if choice not in (ALLOW_UNCHECKED, BLOCK_UNCHECKED):
        raise ConfigError(f"on_nothing_to_check must be '{ALLOW_UNCHECKED}' or '{BLOCK_UNCHECKED}'")
    return choice


def parse_settings(raw: Optional[dict]) -> BigeyeSettings:
    cfg = dashboard_config(raw)
    return BigeyeSettings(
        base_url=resolve_base_url(str(cfg.get("bigeye_url") or DEFAULT_BIGEYE_URL)),
        workspace_id=_workspace_id(cfg.get("workspace_id")),
        auth_scheme=_auth_scheme(cfg.get("auth_scheme")),
        source_name=_optional_str(cfg.get("source_name")),
        warehouse_type=_warehouse_type(cfg.get("warehouse_type")),
        block_on=_block_on(cfg.get("block_on")),
        evaluate_policy=_bool(cfg.get("evaluate_policy", False), "evaluate_policy"),
        timeout_ms=_timeout_ms(cfg.get("bigeye_timeout_ms")),
        agent_id=_optional_str(cfg.get("agent_id")) or DEFAULT_AGENT_ID,
        operation=_optional_str(cfg.get("operation")) or DEFAULT_OPERATION,
        sql_keys=_argument_keys(cfg.get("sql_keys"), "sql_keys", DEFAULT_SQL_KEYS),
        table_keys=_argument_keys(cfg.get("table_keys"), "table_keys", DEFAULT_TABLE_KEYS),
        column_keys=_argument_keys(cfg.get("column_keys"), "column_keys", DEFAULT_COLUMN_KEYS),
        on_nothing_to_check=_on_nothing_to_check(cfg.get("on_nothing_to_check")),
    )
