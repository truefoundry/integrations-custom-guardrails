# Gateway Custom-Guardrail HTTP Contract

This is the verbatim contract between TrueFoundry's AI Gateway and a customer-hosted custom guardrail. The runtime that implements this is `tfy-llm-gateway/src/plugins/custom/guard.ts`. As of commit **`a1c551be`** (PR #2931, May 2026), the contract supports a proper four-state verdict (allow / mutate / block / fail) on top of a 2xx+JSON body. Pre-`a1c551be` history is preserved at the end of this doc for context.

## Where the gateway calls

The gateway POSTs to the URL configured on the Custom Guardrail Config in the dashboard. Each registered config has its own URL. Standard pattern: one Custom Guardrail Config per rail (per-direction), URL pointing at a per-rail endpoint in the wrapper (e.g. `/self-check-input`, `/detect-pii-output`, `/mcp-pre-invoke`).

Rails attach at the LLM hooks (`llm_input`, `llm_output`) or at the MCP tool hooks (`mcp_tool_pre_invoke`, `mcp_tool_post_invoke`). The request shape differs; see "MCP tool pre-invoke" below.

## Request shape

### Input rail (`llm_input` hook)

```json
{
  "requestBody": {
    "model": "openai-main/gpt-4o-mini",
    "messages": [
      {"role": "user", "content": "..."}
    ]
  },
  "context": {
    "user": {"subjectId": "...", "subjectType": "user|serviceaccount|team", "subjectSlug": "..."},
    "metadata": {"request_id": "..."}
  },
  "config": {
    "url": "<this guardrail's URL>",
    "headers": {"...": "custom headers from the dashboard"},
    "config": {"...": "JSON from the dashboard Config field"}
  }
}
```

**The dashboard Config JSON is at `config.config`, not `config`.** The gateway sends the whole guardrail config object (`src/guardrails/guardrailProviderHelpers.ts`: `config: integrationManifest.config`, which is `{url, headers, config}`). Read `config.config` and fall back to the top-level `config`, so direct curl calls with a flat config still work. `config.headers` echoes the guardrail's custom headers, which can hold secrets: never log it unredacted.

### Output rail (`llm_output` hook)

```json
{
  "requestBody":  "<same as input rail>",
  "responseBody": {
    "id": "chatcmpl-...",
    "choices": [{"message": {"role": "assistant", "content": "..."}, "finish_reason": "stop"}],
    "usage": {...},
    "model": "..."
  },
  "context": "<same as input rail>",
  "config":  "<same as input rail>"
}
```

### MCP tool pre-invoke (`mcp_tool_pre_invoke` hook)

Source: `src/mcpGateway/hooks/guardrailsHook.ts`. Verified end to end with `integrations/bigeye/`.

- **Only `tools/call` runs guardrails.** `initialize`, `tools/list` and other JSON-RPC methods pass straight through.
- **`requestBody` is the tool's raw arguments** (JSON-RPC `params.arguments`), e.g. `{"query": "SELECT ..."}`. There are no `messages`. The tool name isn't in the body, so scope a rail to one tool through the policy (below), not the payload.
- **`context` is looser than on LLM hooks.** `metadata` values aren't guaranteed to be strings, and a top-level `context.claims` (verified JWT claims) can appear. The canonical `RequestContext` (`metadata: dict[str, str]`) can reject these payloads with 422; put a lenient model in a separate `mcp_entities.py` and keep `entities.py` identical to `_template`.
- `config` has the same nested shape as on LLM hooks.
- **The tool waits for every Validate verdict.** `verdict: false` means the tool never runs. With `Operation: Mutate`, `result` replaces the tool arguments.

Attach the rail with a guardrail policy, not the `X-TFY-GUARDRAILS` header. The gateway matches `mcpServers` against both `<server>` and `<server>:<tool>`:

```yaml
type: gateway-guardrails-config
name: <policy-name>
when:
  target:
    operator: or
    conditions:
      mcpServers:
        condition: in
        values: [<mcp-server-name>]   # or <mcp-server-name>:<tool-name> for one tool
mcp_tool_pre_invoke_guardrails: [<group>/<config>]
```

`integrations/bigeye/devtools/gateway-examples/` has full YAML for a group, a policy and an MCP server registration. No integration here exercises `mcp_tool_post_invoke` yet; read the source before building one.

### Auth

Bearer or basic, configured in the dashboard. With **Custom Bearer Auth** the gateway sends `Authorization: Bearer <token>`. The wrapper's `WRAPPER_API_KEY` env var must exactly match the dashboard value.

Alternatively, treat the bearer token as the vendor's API key and forward it to the vendor (`integrations/bigeye/` does this). Then there's no separate wrapper key, and each guardrail config carries its own vendor credential. If the vendor URL is configurable, allowlist its hosts: the wrapper is forwarding a caller's credential.

### Timeout

The gateway's guardrail timeout applies: 5000 ms by default (`GUARDRAIL_TIMEOUT_MS` on the gateway; tenants can override it in guardrail settings). It applies to each attempt. Keep the wrapper's own vendor timeout below it, so the wrapper returns a clean 504 before the gateway gives up.

### Retries

The gateway's HTTP client (`src/plugins/utils.ts`, `post`) retries **429 and 503** up to 2 more times with backoff, honouring `Retry-After` up to 5 s. Nothing else is retried. So:

- Never return 429 or 503 from the wrapper. Map a vendor's 429/503 to 502, and a vendor timeout to 504.
- Don't retry inside the wrapper either, or one gateway call fans out into several vendor calls.

## Response shape (what the wrapper returns)

### Allow

```http
HTTP/1.1 200 OK
Content-Type: application/json

{"verdict": true}
```

(`message` is optional and ignored when `verdict: true`.)

### Block

```http
HTTP/1.1 200 OK
Content-Type: application/json

{"verdict": false, "message": "<human-readable reason>"}
```

**HTTP status is still 200.** The `verdict` field is what signals the block. The gateway propagates it to the caller as `guardrail_checks_failed`.

**Callers don't see your `message`.** `guard.ts` sets the check's `explanation` to a fixed `Guardrail violation detected`. The whole wrapper body (`message` and any extra fields) is kept in the check data as `guardrailResponse`, which is where operators read it on the trace. So write `message` for whoever reads traces, and never put secrets or raw user content in the body. Extra fields are a good place for a non-sensitive decision summary (Bigeye returns a `bigeye` block with decisions and latency).

### Mutate (only acted on if dashboard config has `Operation: Mutate`)

```http
HTTP/1.1 200 OK
Content-Type: application/json

{"verdict": true, "transformed": true, "result": {<modified requestBody or responseBody>}}
```

With `Operation: Validate`, mutate fields are ignored.

### Real failure (wrapper crash, missing dependency, etc.)

```http
HTTP/1.1 5xx
Content-Type: application/json

{"error": "...", "detail": "..."}
```

**Non-2xx is reserved for real errors only.** Policy decisions (block) MUST use the 2xx + `verdict: false` pattern, never 4xx.

## Verdict path (the implementation, post `a1c551be`)

The updated handler in `src/plugins/custom/guard.ts`:

```ts
interface GuardrailResponse {
  result?: any | null;
  verdict?: boolean;       // explicit verdict on 2xx
  transformed?: boolean;
  message?: string;
}

// 2xx path:
if (parameters.operation === 'mutate') {
  transformed = response?.transformed === true;
  verdict = response?.verdict === false ? false : true;
} else {
  transformed = false;
  if (typeof response?.verdict === 'boolean') {
    verdict = response.verdict;
  } else if (response?.result === false) {
    verdict = false;
  } else {
    verdict = true;
  }
}

// catch (non-2xx):
verdict = false;
// Routed through the Fail-on-error policy on the guardrail config
```

The wrapper's HTTP status code now carries only "completed vs errored." The verdict lives in the JSON body.

## Enforcing strategy (formerly `Fail on error`)

The dashboard's **Enforcing strategy** decides what a wrapper error means. `src/guardrails/guardrailProviderHelpers.ts` maps `enforce` to `failOnError: true` and `audit` to audit mode; older docs and UIs call `enforce_but_ignore_on_error` "Fail on error: false".

| Enforcing strategy | Wrapper error (non-2xx, timeout, unreachable) | `verdict: false` | Recommended for |
|---|---|---|---|
| `enforce_but_ignore_on_error` | Request continues; trace shows `ignored_error` | Blocks | **Default for most rails** |
| `enforce` | Blocks | Blocks | Safety-critical rails where outages should fail closed |
| `audit` | Request continues | Flagged only, never blocks | Trialling a rail on live traffic |

Key change from pre-`a1c551be`: **rail decisions (`verdict: false` on 200) always block under `enforce` and `enforce_but_ignore_on_error`.** Only real errors are subject to the strategy. Verified end to end on an MCP rail: with the wrapper stopped, `enforce_but_ignore_on_error` let the tool call through.

## Example block response from the gateway

When a wrapper returns `200 {"verdict": false, "message": "Blocked by ..."}`, the gateway propagates:

```json
{
  "status": "failure",
  "message": "Input Guardrail checks failed for integrations: [<group>/<config>]",
  "error": {"type": "guardrail_checks_failed", "code": "400"},
  "guardrail_checks": {
    "input_guardrails": [{
      "guardrail_integration": "<group>/<config>",
      "result": "failed",
      "data": {
        "explanation": "Guardrail violation detected",
        "guardrailUrl": "https://...",
        "guardrailResponse": {"verdict": false, "message": "Blocked by ..."}
      }
    }]
  }
}
```

On MCP hooks the caller gets a JSON-RPC error instead, with `error_type: MCPGuardrailError`, `http_status: 400` and a message like `MCP Pre Tool Call Guardrail checks failed for tool: <tool> :: Input Guardrail checks failed for integrations: [<group>/<config>] - Details: Guardrail violation detected`.

## Selectors and application modes

A registered group can be applied two ways:

1. **Pin to a model**: Models → \<model\> → Guardrails tab → attach group.
2. **Per-request header** `X-TFY-GUARDRAILS` with selectors `<group-name>/<config-name>`. With per-rail endpoints, you'll have multiple selectors in the same group.
3. **Guardrail policy** (`gateway-guardrails-config`) with `when` conditions. MCP tool rails use this, through `mcp_tool_pre_invoke_guardrails` / `mcp_tool_post_invoke_guardrails` (see "MCP tool pre-invoke").

## Hooks beyond input/output

Gateway hooks: `llm_input`, `llm_output`, `mcp_tool_pre_invoke`, `mcp_tool_post_invoke` (internal event types `mcpPreTool` / `mcpPostTool`). The custom-guardrail plugin declares `beforeRequestHook`, `afterRequestHook`, plus the MCP hooks (added in `a0f6211b`). `integrations/bigeye/` is the reference MCP pre-invoke rail.

## What's not in this contract

- **Streaming.** The gateway buffers the full LLM response before calling the output rail.
- **State.** Each call is independent.
- **Bidirectional control.** The wrapper returns one verdict per call.

---

## Pre-`a1c551be` history (deprecated; for context only)

Before May 2026, the handler treated the wrapper's HTTP response as **binary**: any 2xx → `verdict = true`, any non-2xx → `verdict = false`. No way to distinguish a deliberate `HTTP 400` block from a transient `HTTP 5xx` outage. `Fail on error: true` was the only setting that made blocks block — at the cost of also blocking on real outages.

Wrappers written against this contract returned `HTTP 400 + {"error": ..., "message": ..., "activated_rails": [...]}` for blocks. They still work against the new gateway (the 400 is treated as a failure routed through the enforcing strategy), but they should be migrated:

- `enforce_but_ignore_on_error` (`Fail on error: false`) is the correct default with the new contract.
- Real outages can be distinguished from policy decisions in observability.
- The dashboard surfaces an explicit `verdict` field instead of encoding decisions in status codes.

Migration is a one-line change per handler: `JSONResponse(content={...}, status_code=400)` → `ValidateGuardrailResponse(verdict=False, message="...")` (FastAPI serializes as 200+JSON).
