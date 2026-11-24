# `bigeye-guardrails-tfy`

**Status:** tested against Bigeye's live Access Decision API on a catalog with findings, and end to end through a TrueFoundry gateway on an MCP tool pre-invoke policy with the default `block_on`. A table with open issues came back DENY and was blocked before the tool ran, a clean table came back ALLOW and the tool ran, and an unresolved name came back UNSPECIFIED and was allowed. Calls also go through when the wrapper is down (`enforce_but_ignore_on_error`). Not yet tested: sensitive-data findings, policy mode on a workspace with policies, and `UNSPECIFIED` in `block_on` on cataloged assets (see Response contract).

Custom guardrail that asks [Bigeye's Access Decision API](https://docs.bigeye.com/docs/access-decision-api) whether an MCP tool call may touch the warehouse data it names. It attaches to the gateway's `mcp_tool_pre_invoke` hook as a **Validate** guardrail. The gateway waits for the verdict, so a block means the tool never runs.

The wrapper pulls SQL, table names and column names out of the tool arguments and sends them to `POST /api/v1/access-decision`. It blocks when Bigeye's overall decision is in `block_on`, or, if `block_on` includes `UNSPECIFIED`, when any asset is UNSPECIFIED or unresolved. A guardrail can only allow or block, so `REQUIRE_CONFIRMATION` in the default `block_on` is a hard block; remove it to let those calls through.

## Response contract

Per [`docs/gateway-contract.md`](../../docs/gateway-contract.md):

| Status | Body | Meaning |
|---|---|---|
| `200` | `{"verdict": true, "bigeye": {...}}` | Allow: no blocking decision (with the default `block_on`, ALLOW, WARN and UNSPECIFIED all allow), or nothing to check and `on_nothing_to_check` is `allow` |
| `200` | `{"verdict": false, "message": "Bigeye DENY: ...", "bigeye": {...}}` | Block: overall decision is in `block_on`; or `block_on` includes UNSPECIFIED and an asset is UNSPECIFIED or unresolved; or nothing to check and `on_nothing_to_check` is `block` |
| `401` | error JSON | No Bearer token (the Bigeye API key) |
| `422` | error JSON | Payload has the wrong shape. Field locations only; the input is not echoed |
| `500` | error JSON | Misconfiguration, including a `bigeye_url` host that `BIGEYE_ALLOWED_HOSTS` doesn't allow |
| `502` | error JSON | Bigeye non-2xx (including a rejected key), a non-JSON body, or an unrecognised decision value |
| `504` | error JSON | Bigeye did not answer within `bigeye_timeout_ms` |

The wrapper never returns 429 or 503, because the gateway retries those; a 429 or 503 from Bigeye becomes 502. The wrapper itself makes one Bigeye call per request and never retries. Use `enforce_but_ignore_on_error` while you pilot, so Bigeye outages fail open; for production access to regulated data, use `enforce`, so calls are blocked when the check can't run.

Bigeye's own error message is logged only when `LOG_PAYLOADS=true`, because it can quote the SQL. A Bigeye 500 that mentions base64 means it could not decode the key, usually because `auth_scheme` is `bearer` for a personal API key or the key was truncated; the wrapper logs that hint instead.

Bigeye's responses are proto3 JSON, which omits enum fields set to their default. A missing per-asset `accessDecision` counts as UNSPECIFIED. A missing `overallDecision` follows Bigeye's rule: the most severe per-asset decision (DENY > REQUIRE_CONFIRMATION > WARN > ALLOW), ignoring UNSPECIFIED, or UNSPECIFIED when no asset has a decision.

Bigeye reports two kinds of undecided asset. Objects it can't resolve at all, for example because `source_name` matches no data source, are listed in `unresolvedAccessObjects`. Assets it recognizes but hasn't cataloged come back in `accessObjectDecisions` with decision UNSPECIFIED and a source name such as `Unknown Snowflake`. Neither counts toward the overall decision.

`UNSPECIFIED` in `block_on` blocks when the overall decision is UNSPECIFIED, or when any asset is UNSPECIFIED or unresolved, whether or not Bigeye sent an overall decision. Use it to block calls that touch data Bigeye can't vouch for. Check Bigeye's responses before relying on it: in testing, Bigeye left out `accessDecision` on every resolved asset, including on DENY and ALLOW calls, so every asset read as UNSPECIFIED and this setting would have blocked every call.

The `bigeye` summary lands on the gateway trace span. It looks like `{checked, span_external_id, overall_decision, assets: [{name, decision}], unresolved_count, assets_found, latency_ms}`, or `{checked: false, reason}` when there was nothing to check. `assets_found` is the per-asset decisions plus unresolved objects, so an SQL check where Bigeye found nothing shows 0. SQL text and data values are never included; SQL assets are named `<sql>`.

## Endpoints

```
GET  /health                  health check
GET  /debug/loaded-config     non-secret runtime summary
POST /mcp-pre-invoke          MCP tool pre-invoke rail (Operation: Validate)
```

## Auth

The guardrail config's Bearer token is the Bigeye API key. The wrapper forwards it as `Authorization: <auth_scheme> <key>` and stores nothing, so there is no separate wrapper key.

`auth_scheme` defaults to `apikey`. Bigeye's docs specify `Authorization: apikey <key>` for personal API keys, and its API rejects them when sent as Bearer. Bigeye recommends a service-account key scoped to the target workspace; end users are identified by `username`, not by their own Bigeye credentials. The key needs at least Edit access to the target workspace.

The wrapper only calls https hosts that `BIGEYE_ALLOWED_HOSTS` allows. The default, `*.bigeye.com`, covers `app.bigeye.com` and dedicated `<company>.bigeye.com` stacks. It doesn't follow redirects.

## Config JSON

Read from `config.config` (dashboard Config JSON), falling back to top-level `config`.

| Key | Default | Notes |
|---|---|---|
| `bigeye_url` | `https://app.bigeye.com` | Host must be allowed by `BIGEYE_ALLOWED_HOSTS` |
| `workspace_id` | required | |
| `auth_scheme` | `apikey` | `apikey` or `bearer`. Bigeye rejects personal API keys sent as Bearer |
| `source_name` | none | Bigeye data source name. Required when arguments contain SQL |
| `warehouse_type` | none | Bigeye `DATABASE_TYPE_*` value, e.g. `DATABASE_TYPE_SNOWFLAKE`, sent as `sourceType`. `WAREHOUSE_TYPE_*` (from Bigeye's doc examples) and plain enum names such as `snowflake` (any case) are mapped to `DATABASE_TYPE_*`; other values are a configuration error. Omitted when unset |
| `block_on` | `["DENY", "REQUIRE_CONFIRMATION"]` | Short or `ACCESS_DECISION_` names. `UNSPECIFIED` also blocks undecided or unresolved assets (see above) |
| `evaluate_policy` | `false` | Bigeye policy mode (see below) |
| `bigeye_timeout_ms` | `3500` | Keep below the gateway's guardrail timeout |
| `agent_id` | `truefoundry-gateway` | `agentExternalId` for user callers |
| `operation` | `mcp_tool_call` | Shown in Bigeye's UI |
| `sql_keys` | `["query", "sql", "statement"]` | Top-level argument names that hold SQL. The first non-empty one is used |
| `table_keys` | `["tables", "table"]` | Top-level argument names that hold table names |
| `column_keys` | `["columns", "column"]` | Top-level argument names that hold column names |
| `on_nothing_to_check` | `allow` | `allow` or `block` when no configured argument holds SQL, tables or columns (see below) |

### Argument names

The wrapper reads only top-level tool arguments, under the names in `sql_keys`, `table_keys` and `column_keys`. Nested arguments such as `{"params": {"query": ...}}` are not read. Setting one of these lists replaces its default; the other two keep theirs. The defaults follow Bigeye's Kong plugin, plus `statement`. Name values may be a list, a JSON-array string or a comma-separated string.

If your MCP server's SQL argument has another name, add it to `sql_keys`. Otherwise the wrapper finds nothing to check on that server's calls.

### Nothing to check

When no configured argument holds SQL, tables or columns, the wrapper doesn't call Bigeye. It logs a warning, puts `{checked: false, reason}` in the summary, and then allows the call (`"on_nothing_to_check": "allow"`, the default) or blocks it (`"block"`).

Use `"block"` only with a policy scoped to the SQL tool itself, through an `mcpServers` value of `<mcp-server-name>:<tool-name>`. On a whole server it blocks every tool that isn't a SQL tool.

### Identity

For users, `username` is the subject slug; for virtual accounts and agents, the slug becomes `agentExternalId`.

### Policy mode

Per the Policy Evaluation section of Bigeye's [Access Decision API docs](https://docs.bigeye.com/docs/access-decision-api):

- With `evaluate_policy` false (the default), the verdict comes from catalog signals. Bigeye still evaluates workspace policies asynchronously after the response and shows the results in Agent Trust Hub; they don't affect the verdict.
- With `evaluate_policy: true`, policies are enforced inline and the overall decision comes only from the policy engine: DENY if any policy matched, ALLOW otherwise. Sensitive-data findings and open issues don't count in that mode. Inline evaluation adds latency that grows with the number of workspace policies, so benchmark it against `bigeye_timeout_ms`, which must stay below the gateway's guardrail timeout.
- Bigeye's `access.policy.always_evaluate_synchronously` Advanced Setting forces inline evaluation on every request, regardless of `evaluate_policy`.

## Runtime env

| Var | Default | |
|---|---|---|
| `BIGEYE_ALLOWED_HOSTS` | `*.bigeye.com` | Comma-separated, https only. Exact hostnames, or `*.domain` for any subdomain of `domain` (not `domain` itself) |
| `LOG_PAYLOADS` | `false` | Log incoming payloads with identity values redacted |
| `LOG_LEVEL` | `info` | |

## Local run and tests

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
.venv/bin/uvicorn main:app --reload --port 8000
```

## Testing without a TrueFoundry tenant

All three checks run on your machine. Only the last two need a Bigeye API key.

```bash
.venv/bin/pytest -q
cp .env.example .env   # then fill in the smoke-test section
.venv/bin/python devtools/smoke_test.py
.venv/bin/python devtools/local_wrapper_check.py --scheme apikey
```

- `pytest`: the full suite, with Bigeye mocked. No network or credentials.
- `devtools/smoke_test.py`: calls Bigeye directly, using the wrapper's request builder. It checks the key under both auth schemes, then sends TABLE, COLUMN, SQL and SQL-with-policy decisions. It needs your Bigeye URL, key, workspace, data source, a table, a column and a SQL statement in `.env`. Decisions are recorded under agent `truefoundry-smoke-test` (set `BIGEYE_SMOKE_AGENT_ID` to change it); `--list` shows them.
- `devtools/local_wrapper_check.py`: starts the wrapper on 127.0.0.1 and sends it gateway-shaped payloads. It checks health, that the SQL verdict matches `block_on` for whatever decision Bigeye returns, a forced block, nothing to check, a wrong key and a disallowed host. It fails if the key appears in the wrapper's log.

## Test kit (`devtools/`)

- `smoke_test.py` and `local_wrapper_check.py`: see above.
- `stub-sql-mcp/`: an optional test-only FastMCP server (`run_query`, `describe_table`) that logs every invocation and returns canned rows.
- `gateway-examples/`: YAML with placeholder values for the guardrail group, a policy, and registering an MCP server such as the stub.

## Register in TrueFoundry

1. Deploy the wrapper where the gateway can reach it over HTTPS (see Deploy).
2. Add a guardrails group with a **Custom** guardrail config. Add one config per Bigeye data source, because `source_name` and `warehouse_type` apply to every object in a call.
   - **URL**: `https://<wrapper-host>/mcp-pre-invoke`
   - **Auth Data**: Custom Bearer Auth, with the Bigeye API key as the token
   - **Operation**: Validate
   - **Enforcing strategy**: `enforce_but_ignore_on_error` while you pilot, so a Bigeye outage doesn't block tool calls. For production access to regulated data, use `enforce`, so calls are blocked when the check can't run
   - **Config**:

     ```json
     {
       "bigeye_url": "https://<your-stack>.bigeye.com",
       "workspace_id": "<bigeye-workspace-id>",
       "source_name": "<bigeye-data-source-name>",
       "warehouse_type": "<warehouse-type, e.g. snowflake>",
       "block_on": ["DENY", "REQUIRE_CONFIRMATION"],
       "sql_keys": ["query", "sql", "statement"],
       "on_nothing_to_check": "allow"
     }
     ```

3. Add a guardrail policy that runs the config before tool calls on your MCP server, using an `mcpServers` condition (abridged):

   ```yaml
   type: gateway-guardrails-config
   name: bigeye-access-decision
   when:
     target:
       operator: or
       conditions:
         mcpServers:
           condition: in
           values: [<mcp-server-name>]   # or <mcp-server-name>:<tool-name> for a single tool
   mcp_tool_pre_invoke_guardrails: [<guardrail-group>/<guardrail-config>]
   ```

`devtools/gateway-examples/` has the full YAML for the group, the policy and an MCP server registration; apply each with `tfy apply -f <file>`.

## Deploy

```bash
.venv/bin/pip install -U truefoundry
tfy login
.venv/bin/python deploy.py --wait
```

The wrapper is a plain Docker container on port 8000. Any host the gateway can reach over HTTPS works.
