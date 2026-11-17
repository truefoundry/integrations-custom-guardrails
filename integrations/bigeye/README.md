# `bigeye-guardrails-tfy`

Custom guardrail that asks [Bigeye's Access Decision API](https://docs.bigeye.com/docs/access-decision-api) whether an MCP tool call may touch the warehouse data it names. It attaches to the gateway's `mcp_tool_pre_invoke` hook as a **Validate** guardrail. The gateway waits for the verdict, so a block means the tool never runs.

The wrapper pulls SQL, table names and column names out of the tool arguments, sends them to `POST /api/v1/access-decision`, and blocks when Bigeye's overall decision is in `block_on`.

## Response contract

Per [`docs/gateway-contract.md`](../../docs/gateway-contract.md):

| Status | Body | Meaning |
|---|---|---|
| `200` | `{"verdict": true, "bigeye": {...}}` | Allow: overall decision not in `block_on` (by default ALLOW, WARN, UNSPECIFIED), or nothing to check |
| `200` | `{"verdict": false, "message": "Bigeye DENY: ...", "bigeye": {...}}` | Block: overall decision is in `block_on`, or `block_on` includes UNSPECIFIED and an asset is UNSPECIFIED or unresolved |
| `401` | error JSON | No Bearer token (the Bigeye API key) |
| `422` | error JSON | Payload has the wrong shape. Field locations only; the input is not echoed |
| `500` | error JSON | Misconfiguration, including a `bigeye_url` host outside `BIGEYE_ALLOWED_HOSTS` |
| `502` | error JSON | Bigeye non-2xx (including a rejected key), a non-JSON body, or an unrecognised decision value |
| `504` | error JSON | Bigeye did not answer within `bigeye_timeout_ms` |

The wrapper never returns 429 or 503, because the gateway retries those; a 429 or 503 from Bigeye becomes 502. The wrapper itself makes one Bigeye call per request and never retries. Use `enforce_but_ignore_on_error` so Bigeye outages fail open.

Bigeye's own error message is logged only when `LOG_PAYLOADS=true`, because it can quote the SQL. A Bigeye 500 that mentions base64 means it could not decode the key, usually because `auth_scheme` is `bearer` for a personal API key or the key was truncated; the wrapper logs that hint instead.

Bigeye's responses are proto3 JSON, which omits enum fields set to their default. A missing per-asset `accessDecision` counts as UNSPECIFIED. A missing `overallDecision` follows Bigeye's rule: the most severe per-asset decision (DENY > REQUIRE_CONFIRMATION > WARN > ALLOW), ignoring UNSPECIFIED, or UNSPECIFIED when no asset has a decision.

`UNSPECIFIED` in `block_on` blocks when the overall decision is UNSPECIFIED, or when any asset is UNSPECIFIED or unresolved, whether or not Bigeye sent an overall decision. Use it to block calls that touch data Bigeye can't vouch for.

The `bigeye` summary lands on the gateway trace span. It looks like `{checked, span_external_id, overall_decision, assets: [{name, decision}], unresolved_count, assets_found, latency_ms}`, or `{checked: false, reason}` when there was nothing to check. `assets_found` is the per-asset decisions plus unresolved objects, so an SQL check where Bigeye found nothing shows 0. SQL text and data values are never included; SQL assets are named `<sql>`.

## Endpoints

```
GET  /health                  health check
GET  /debug/loaded-config     non-secret runtime summary
POST /mcp-pre-invoke          MCP tool pre-invoke rail (Operation: Validate)
```

## Auth

The guardrail config's Bearer token is the Bigeye API key. The wrapper forwards it as `Authorization: <auth_scheme> <key>` and stores nothing, so there is no separate wrapper key. It only calls https hosts listed exactly in `BIGEYE_ALLOWED_HOSTS` (default `app.bigeye.com,demo.bigeye.com`), and it doesn't follow redirects.

## Config JSON

Read from `config.config` (dashboard Config JSON), falling back to top-level `config`.

| Key | Default | Notes |
|---|---|---|
| `bigeye_url` | `https://app.bigeye.com` | Host must be in `BIGEYE_ALLOWED_HOSTS` |
| `workspace_id` | required | |
| `auth_scheme` | `apikey` | `apikey` or `bearer` |
| `source_name` | none | Bigeye data source name. Required when arguments contain SQL |
| `warehouse_type` | none | Bigeye `DATABASE_TYPE_*` value, e.g. `DATABASE_TYPE_SNOWFLAKE`, sent as `sourceType`. `WAREHOUSE_TYPE_*` (from Bigeye's doc examples) and plain enum names such as `snowflake` (any case) are mapped to `DATABASE_TYPE_*`; other values are a configuration error. Omitted when unset |
| `block_on` | `["DENY", "REQUIRE_CONFIRMATION"]` | Short or `ACCESS_DECISION_` names. `UNSPECIFIED` also blocks undecided or unresolved assets (see above) |
| `evaluate_policy` | `false` | Bigeye policy mode (see below) |
| `bigeye_timeout_ms` | `3500` | Keep below the gateway's guardrail timeout |
| `agent_id` | `truefoundry-gateway` | `agentExternalId` for user callers |
| `operation` | `mcp_tool_call` | Shown in Bigeye's UI |

Keep `evaluate_policy` false unless you rely on Bigeye policies. In policy mode Bigeye decides from policies only, ignores sensitivity and open-issue signals, and returns ALLOW when no policy matches.

Argument keys follow Bigeye's Kong plugin: `query`/`sql`, `tables`/`table`, `columns`/`column`. Name values may be a list, a JSON-array string or a comma-separated string. For users, `username` is the subject slug; for virtual accounts and agents, the slug becomes `agentExternalId`.

## Runtime env

| Var | Default | |
|---|---|---|
| `BIGEYE_ALLOWED_HOSTS` | `app.bigeye.com,demo.bigeye.com` | Exact hostnames |
| `LOG_PAYLOADS` | `false` | Log incoming payloads with identity values redacted |
| `LOG_LEVEL` | `info` | |

## Local run and tests

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
.venv/bin/uvicorn main:app --reload --port 8000
```

## Test kit (`devtools/`)

- `smoke_test.py`: live check against Bigeye using the wrapper's request builder. It reads `BIGEYE_*` from `.env`; `--list` shows stored decisions and `--snake` sends snake_case keys.
- `stub-sql-mcp/`: a test-only FastMCP server (`run_query`, `describe_table`) that logs every invocation and returns canned rows.
- `devtest/`: YAML for the stub registration, the `bigeye` guardrail group, and the policy that attaches it to the stub.

## Deploy

```bash
.venv/bin/pip install -U truefoundry
tfy login
.venv/bin/python deploy.py --wait
```

The wrapper is a plain Docker container on port 8000. Any host the gateway can reach over HTTPS works.
