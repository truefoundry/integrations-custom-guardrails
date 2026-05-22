# TrueFoundry Custom-Guardrail HTTP Contract

The single source of truth for the contract every integration in this repo must satisfy. This is what `tfy-llm-gateway/src/plugins/custom/guard.ts` reads on every guardrail call.

As of `tfy-llm-gateway` commit **`a1c551be`** (PR #2931, May 2026), the contract is **2xx + JSON verdict**. Older 4xx-block paths are legacy; see "Pre-`a1c551be` history" at the end.

## How the gateway calls the wrapper

The gateway POSTs to the URL configured on each Custom Guardrail Config in the dashboard. Standard pattern: **one Custom Guardrail Config per rail per direction**, URL pointing at a per-rail endpoint in the wrapper (e.g. `/self-check-input`, `/detect-pii-output`).

Authentication: **Custom Bearer Auth** in the dashboard → gateway sends `Authorization: Bearer <token>`. The wrapper's `WRAPPER_API_KEY` env var must match the dashboard value exactly.

Timeout: 10 seconds default, configurable on the guardrail config.

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
    "user": {
      "subjectId": "...",
      "subjectType": "user|serviceaccount|team",
      "subjectSlug": "..."
    },
    "metadata": {"request_id": "..."}
  },
  "config": {"...": "JSON from the dashboard Config field"}
}
```

### Output rail (`llm_output` hook)

```json
{
  "requestBody":  "<same shape as input rail>",
  "responseBody": {
    "id": "chatcmpl-...",
    "choices": [
      {"message": {"role": "assistant", "content": "..."}, "finish_reason": "stop"}
    ],
    "usage": {...},
    "model": "..."
  },
  "context": "<same as input rail>",
  "config":  "<same as input rail>"
}
```

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

**HTTP status is still 200.** The `verdict` field is the block signal. The gateway propagates to the caller as `guardrail_checks_failed` with the wrapper's `message` preserved.

### Mutate (only acted on if dashboard config has `Operation: Mutate`)

```http
HTTP/1.1 200 OK
Content-Type: application/json

{"verdict": true, "transformed": true, "result": {<modified requestBody or responseBody>}}
```

With `Operation: Validate`, mutate fields are ignored. For input rails the `result` field replaces `requestBody`. For output rails it replaces `responseBody`.

### Real failure (wrapper crash, missing dependency, etc.)

```http
HTTP/1.1 5xx
Content-Type: application/json

{"error": "...", "detail": "..."}
```

**Non-2xx is reserved for real errors only.** Policy decisions (block) MUST use the 2xx + `verdict: false` pattern, never 4xx.

## Pydantic models

Every integration carries its own copy of these. They live at `integrations/<vendor>/entities.py`. They are not shared because contract drift between integrations is rare and shared imports add complexity.

```python
from typing import Any, Optional
from pydantic import BaseModel


class ValidateGuardrailResponse(BaseModel):
    """Response body for validate-operation guardrails."""
    verdict: bool
    message: Optional[str] = None


class MutateGuardrailResponse(BaseModel):
    """Response body for mutate-operation guardrails."""
    verdict: bool
    transformed: bool
    result: dict[str, Any]


class RequestContext(BaseModel):
    user: dict
    metadata: Optional[dict[str, str]] = None


class InputGuardrailRequest(BaseModel):
    requestBody: dict
    context: RequestContext
    config: Optional[dict] = None


class OutputGuardrailRequest(BaseModel):
    requestBody: dict
    responseBody: dict
    config: Optional[dict] = None
    context: RequestContext
```

**If the gateway contract changes**: update `entities.py` in EVERY `integrations/<vendor>/` directory in a coordinated PR. Add a note in the PR description so reviewers know to check every integration touches.

## Why no shared code

This repo deliberately avoids a `shared/` directory. Each integration carries its own copies of `entities.py`, auth helpers, and message extractors. Reasons:

1. **Per-vendor dependency pins**. NeMo wants `nemoguardrails 0.21+`; Guardrails AI wants pinned-from-GitHub `0.9.3`. Forcing a shared venv would mean fighting transitive dep conflicts.
2. **Independent deployability**. Each integration is a separate TFY Service. A bad commit in the shared `entities.py` cannot break someone else's deploy if there is no shared `entities.py`.
3. **Discoverability over DRY**. New integrators benefit more from "copy the working `_template/` and edit" than from "trace through a shared abstraction." The duplication is bounded (~200 lines across two integrations today).
4. **Per-integration freedom to diverge**. If a future vendor's message-shape handling needs a different `last_user_text` (e.g. for MCP payloads), that integration can change its copy without coordinating with others.

The cost is contract drift: if `entities.py` evolves in one integration but not another, they're out of sync. Mitigations:
- This doc is the single source of truth. All integrations match it verbatim.
- PR reviews enforce: when changing `entities.py`, update every integration's copy.
- CI (when added) can `diff` each integration's `entities.py` against this doc's snippet.

## Gateway-side selector format

After registering Custom Guardrail Configs in the dashboard, callers attach them via the `X-TFY-GUARDRAILS` header:

```python
extra_headers={
    "X-TFY-GUARDRAILS": json.dumps({
        "llm_input_guardrails":  ["<group-name>/<config-name>"],
        "llm_output_guardrails": ["<group-name>/<config-name>"],
    })
}
```

Selector format is `<group-name>/<config-name>` — the human-readable names from the dashboard. With per-rail endpoints, you have multiple selectors in the same group.

## `Fail on error` configuration

| `Fail on error` | Behavior on real 5xx | Recommended for |
|---|---|---|
| `false` | Pass-through | **Default for most rails** — rail decisions (block) and outages (pass) are distinguishable |
| `true` | Block | Safety-critical rails where transient outages should fail-closed |

**Rail decisions (`verdict: false` on 200) always block regardless of `Fail on error`.** Only true 5xx outages are subject to failOnError.

## Pre-`a1c551be` history (deprecated; context only)

Before May 2026, the handler treated any 2xx as "passed" and any non-2xx (4xx and 5xx alike) as "failed." Wrappers returned `HTTP 400 + {"error": "...", "message": "..."}` for blocks, and `Fail on error: true` was the only way to make blocks block — with the side effect of also blocking on transient outages.

Both wrappers in this repo were restructured off this pattern. If you're touching an old wrapper still using the 4xx-block shape:
1. Update `entities.py` to include `ValidateGuardrailResponse` / `MutateGuardrailResponse`.
2. Change handlers from `JSONResponse(content={...}, status_code=400)` to `ValidateGuardrailResponse(verdict=False, message="...")`.
3. Flip dashboard `Fail on error` to `false`.

Smoke-test the round trip before declaring done. On a pre-`a1c551be` gateway, the new shape will silently fail (gateway treats 200+verdict=false as "passed") — verify the tenant gateway version first.
