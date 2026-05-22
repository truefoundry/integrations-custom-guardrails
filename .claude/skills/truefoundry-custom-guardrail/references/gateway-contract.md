# Gateway Custom-Guardrail HTTP Contract

This is the verbatim contract between TrueFoundry's AI Gateway and a customer-hosted custom guardrail. The runtime that implements this is `tfy-llm-gateway/src/plugins/custom/guard.ts`. As of commit **`a1c551be`** (PR #2931, May 2026), the contract supports a proper four-state verdict (allow / mutate / block / fail) on top of a 2xx+JSON body. Pre-`a1c551be` history is preserved at the end of this doc for context.

## Where the gateway calls

The gateway POSTs to the URL configured on the Custom Guardrail Config in the dashboard. Each registered config has its own URL. Standard pattern: one Custom Guardrail Config per rail (per-direction), URL pointing at a per-rail endpoint in the wrapper (e.g. `/self-check-input`, `/detect-pii-output`).

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
  "config": {"...": "JSON from the dashboard Config field"}
}
```

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

### Auth

Bearer or basic, configured in the dashboard. With **Custom Bearer Auth** the gateway sends `Authorization: Bearer <token>`. The wrapper's `WRAPPER_API_KEY` env var must exactly match the dashboard value.

### Timeout

Default 10 seconds. Configurable on the guardrail config.

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

**HTTP status is still 200.** The `verdict` field is what signals the block. The gateway propagates to the caller as `guardrail_checks_failed` with the wrapper's `message` preserved.

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

## `Fail on error` configuration

| `Fail on error` | Behavior on real 5xx | Recommended for |
|---|---|---|
| `false` | Pass-through | **Default for most rails** — rail decisions (block) and outages (pass) are now distinguishable |
| `true` | Block | Safety-critical rails where transient outages should fail-closed |

Key change from pre-`a1c551be`: **rail decisions (`verdict: false` on 200) always block regardless of `Fail on error`.** Only true 5xx outages are subject to failOnError.

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
        "verdict": false,
        "explanation": "<wrapper's `message` field>",
        "guardrailUrl": "https://..."
      }
    }]
  }
}
```

## Selectors and application modes

A registered group can be applied two ways:

1. **Pin to a model**: Models → \<model\> → Guardrails tab → attach group.
2. **Per-request header** `X-TFY-GUARDRAILS` with selectors `<group-name>/<config-name>`. With per-rail endpoints, you'll have multiple selectors in the same group.

## Hooks beyond input/output

Gateway hooks: `llm_input`, `llm_output`, `mcp_pre_tool`, `mcp_post_tool`. The custom-guardrail plugin declares `beforeRequestHook`, `afterRequestHook`, plus the MCP hooks (added in `a0f6211b`). Most custom guardrails won't need MCP hooks for v1.

## What's not in this contract

- **Streaming.** The gateway buffers the full LLM response before calling the output rail.
- **State.** Each call is independent.
- **Bidirectional control.** The wrapper returns one verdict per call.

---

## Pre-`a1c551be` history (deprecated; for context only)

Before May 2026, the handler treated the wrapper's HTTP response as **binary**: any 2xx → `verdict = true`, any non-2xx → `verdict = false`. No way to distinguish a deliberate `HTTP 400` block from a transient `HTTP 5xx` outage. `Fail on error: true` was the only setting that made blocks block — at the cost of also blocking on real outages.

Wrappers written against this contract returned `HTTP 400 + {"error": ..., "message": ..., "activated_rails": [...]}` for blocks. They still work against the new gateway (the 400 is treated as a failure routed through `Fail on error`), but they should be migrated:

- `Fail on error: false` is the correct default with the new contract.
- Real outages can be distinguished from policy decisions in observability.
- The dashboard surfaces an explicit `verdict` field instead of encoding decisions in status codes.

Migration is a one-line change per handler: `JSONResponse(content={...}, status_code=400)` → `ValidateGuardrailResponse(verdict=False, message="...")` (FastAPI serializes as 200+JSON).
