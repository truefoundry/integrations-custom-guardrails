# Design notes

How and why the Lasso Security wrapper is shaped the way it is. Read [`../README.md`](../README.md) for the quickstart; this doc is for "I'm about to change something non-trivial and need the context."

## Problem

The TrueFoundry AI Gateway lets customers plug in custom guardrails over a simple HTTP contract (post `tfy-llm-gateway` commit `a1c551be`, May 2026):

```
POST <user-server>/<endpoint>
{ requestBody, [responseBody], context, config }
  -> 200 {"verdict": true}                     => pass through
  -> 200 {"verdict": false, "message": "..."}  => block
  -> 200 {verdict: true, transformed: true, result: {...}}  => mutate (Operation=Mutate only)
  -> 5xx error                                 => guardrail failure (per failOnError)
```

We want to use **[Lasso Security](https://server.lasso.security)** behind that contract. Lasso exposes a hosted API v3 with two operations relevant here:

- **`classify`** — evaluate messages against deputies configured in the Lasso console; return findings with actions (`BLOCK`, `WARN`, etc.).
- **`classifix`** — same evaluation plus optional message rewriting / span masking for PII and similar violations.

The wrapper translates between the gateway's OpenAI-shaped bodies and Lasso's JSON API, then maps Lasso findings back to `ValidateGuardrailResponse` or `MutateGuardrailResponse`.

## Why a thin Python wrapper (vs. alternatives)

Three integration paths were considered. See the team's "Gateway Integrations - Support SOP":

| Path | What | Verdict |
|---|---|---|
| **A. Custom guardrail wrapper** (this repo) | FastAPI service that POSTs to Lasso API v3 per rail. | **Chosen.** Zero changes to `tfy-llm-gateway`. Ships now. |
| B. Lasso as a Custom Endpoint | Register Lasso's own server URL as the model endpoint. | Wrong shape — Lasso is not the LLM; guardrails must run at the gateway hooks. |
| C. Native plugin in `tfy-llm-gateway` | Add `src/plugins/lasso-security/`. | Cross-repo work; defer until demand justifies native §5 promotion. |

Lasso is SaaS-only with a structured HTTP API — a strong fit for the custom-guardrail path while validating product-market fit.

## Architecture

```
   ┌──────────────────────────────────────────────────┐
   │           TrueFoundry AI Gateway                 │
   │   (input hook → LLM → output hook)               │
   └──────────────────────────────────────────────────┘
         │                    │                    │
         │ POST /lasso-classify│                    │ POST /lasso-classify-output
         │ POST /lasso-classifix                    │ POST /lasso-classifix-output
         ▼                    ▼                    ▼
   ┌──────────────────────────────────────────────────┐
   │   lasso-guardrails-tfy (this service)            │
   │   FastAPI (root-level main.py + entities.py)     │
   │     /health  /debug/runtime-config               │
   │                                                  │
   │   Validate rails:                                │
   │     POST /lasso-classify         → classify PROMPT      │
   │     POST /lasso-classify-output  → classify COMPLETION  │
   │                                                  │
   │   Mutate rails:                                  │
   │     POST /lasso-classifix        → classifix PROMPT       │
   │     POST /lasso-classifix-output → classifix COMPLETION   │
   │                                                  │
   │   guardrail/lasso.py — shared Lasso client + mapping │
   └──────────────────────────────────────────────────┘
                          │
                          ▼ HTTPS
   ┌──────────────────────────────────────────────────┐
   │   Lasso Security API v3                          │
   │   POST {api_base}/classify | {api_base}/classifix│
   │   Auth: lasso-api-key header                     │
   └──────────────────────────────────────────────────┘
```

**Four endpoints, not composite.** Each rail direction and operation gets its own POST route and its own Custom Guardrail Config in the dashboard. Users can attach validate-only rails, mutate-only rails, or both.

**SaaS round-trip per request.** Unlike NeMo (judge LLM via gateway) or Guardrails AI (local validators), every rail call hits `server.lasso.security`. Budget latency accordingly (default timeout 10s).

## Request flow

### Validate (`classify`)

`POST /lasso-classify` (output rail is identical except it reads `responseBody.choices[].message.content`):

1. Extract OpenAI-format `messages` from `requestBody`.
2. Build Lasso payload: `messageType=PROMPT`, `sessionId`, optional `userId`, `tools` from request body.
3. `POST {LASSO_API_BASE}/classify` with `lasso-api-key` header.
4. Scan `findings` for any deputy finding with `action == "BLOCK"`.
5. If blocked → `ValidateGuardrailResponse(verdict=False, message=...)`.
6. If `violations_detected` but no BLOCK (e.g. WARN only) → allow (`verdict=True`).
7. Empty messages → short-circuit allow.

### Mutate (`classifix`)

`POST /lasso-classifix` (and output variant):

1. Same extraction and Lasso call, but `endpoint=classifix`.
2. If BLOCK findings lack `start`/`end`/`mask` span metadata → **block** (`verdict=False`, `transformed=False`, original body in `result`).
3. Else apply masks in order:
   - Prefer Lasso-returned `messages` array when present.
   - Else apply per-finding span masks onto `requestBody.messages` or `responseBody.choices`.
4. If content changed → `MutateGuardrailResponse(verdict=True, transformed=True, result=<masked body>)`.
5. If no transformation needed → `verdict=True, transformed=False`.

## Verdict mapping

| Lasso signal | Validate rail | Mutate rail |
|---|---|---|
| No violations | allow | allow, `transformed=false` |
| WARN-only violations | allow (logged) | allow, `transformed=false` |
| BLOCK with mask spans / rewritten messages | block | mask in place, `transformed=true` |
| BLOCK without mask metadata | block | block |

Block messages look like: `Lasso guardrail blocked: {deputy}/{finding_name} ({severity})`.

## Configuration surface

| Source | Keys | Purpose |
|---|---|---|
| Deploy env / TFY secret | `LASSO_API_KEY` | Default API key for all calls |
| Deploy env | `LASSO_API_BASE` | Override API base (default `https://server.lasso.security/gateway/v3`) |
| Dashboard Config JSON | `credentials.apiKey` | Per-config API key override |
| Dashboard Config JSON | `api_base`, `timeout`, `sessionId`, `userId`, `conversationId` | Optional per-request overrides |
| Gateway context | `context.metadata.session_id` / `sessionId` / `lasso-conversation-id` | Session continuity for Lasso |
| Gateway context | `context.user.subjectSlug` / `subjectId` | Maps to Lasso `userId` |

Wrapper bearer auth (`WRAPPER_API_KEY`) is independent of the Lasso API key.

## Repo layout

```
lasso-guardrails-tfy/
├── main.py                 FastAPI app: routes, bearer auth, /debug/runtime-config
├── entities.py             Pydantic models (validate + mutate response types)
├── guardrail/
│   ├── __init__.py
│   └── lasso.py            Lasso HTTP client, verdict/mutate mapping, four handlers
├── deploy.py               TFY Python SDK deployment manifest
├── Dockerfile
├── requirements.txt
├── .env.example
└── docs/
    ├── DESIGN.md           (this file)
    └── public-docs-lasso-security.md
```

Unlike NeMo and Guardrails AI, rail handlers live in a single `guardrail/lasso.py` module because all four endpoints share the same HTTP client and mapping logic.

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| 401 from wrapper | Bearer token mismatch | Sync `WRAPPER_API_KEY` secret, pod env, dashboard Custom Bearer Auth |
| 500 "Lasso API key not configured" | Missing `LASSO_API_KEY` secret and no `config.credentials.apiKey` | Create secret; redeploy; or set Config JSON |
| 401 from wrapper with invalid-key message | Bad Lasso API key | Rotate key in Lasso console; update TFY secret |
| Gateway allows but Lasso console shows BLOCK | WARN-only finding | Expected — only `action: BLOCK` denies on validate rails |
| Mutate rail blocks instead of masking | BLOCK finding without span metadata | Lasso deputy config; or use validate rail to hard-block |
| Stale code after redeploy | TFY image cache | `curl /debug/runtime-config` and check `wrapper_version` |

## Future work

1. Pytest smoke suite (`tests/test_smoke.py`) with mocked Lasso responses — pattern from `integrations/_template/`.
2. Per-tenant deputy selection via `config` if Lasso exposes that in API v3.
3. Native plugin promotion (SOP §5) if Lasso becomes a strategic integration — HTTP mapping logic stays the same.
