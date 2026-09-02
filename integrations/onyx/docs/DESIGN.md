# Design notes

How and why the Onyx Security wrapper is shaped the way it is. Read [`../README.md`](../README.md) for the quickstart; this doc is for "I'm about to change something non-trivial and need the context."

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

We want to use **[Onyx AI Guard](https://onyx.security)** behind that contract. Onyx's integration guide exposes a `/simple` evaluate API: send extracted text (`user_prompt` or `response`), get back `action` (`allow` / `block` / `modify`) plus optional `custom_popup_message`. Auth is the **policy token in the URL path** — no Authorization header to Onyx.

The wrapper extracts text from the gateway body, calls `/simple`, and maps `action` to `ValidateGuardrailResponse`.

## Why a thin Python wrapper (vs. alternatives)

Three integration paths were considered:

| Path | What | Verdict |
|---|---|---|
| **A. Custom guardrail wrapper** (this repo) | FastAPI service that POSTs to Onyx AI Guard `/simple` per rail. | **Chosen.** Zero changes to `tfy-llm-gateway`. Ships now. |
| B. Onyx as a Custom Endpoint | Register Onyx's own server URL as the model endpoint. | Wrong shape — Onyx is not the LLM; guardrails must run at the gateway hooks. |
| C. Native plugin in `tfy-llm-gateway` | Add `src/plugins/onyx/`. | Cross-repo work; defer until demand justifies a native gateway plugin. |

Onyx is SaaS-only with a structured HTTP API — a strong fit for the custom-guardrail path while validating product-market fit. Closest sibling in this repo: `integrations/lasso-security/`.

## Architecture

```
   ┌──────────────────────────────────────────────────┐
   │           TrueFoundry AI Gateway                 │
   │   (input hook → LLM → output hook)               │
   └──────────────────────────────────────────────────┘
         │                                      │
         │ POST /onyx-input                     │ POST /onyx-output
         ▼                                      ▼
   ┌──────────────────────────────────────────────────┐
   │   onyx-guardrails-tfy (this service)             │
   │   FastAPI (root-level main.py + entities.py)     │
   │     /health  /debug/loaded-config                │
   │                                                  │
   │   Validate rails:                                │
   │     POST /onyx-input   → {"user_prompt": "..."}  │
   │     POST /onyx-output  → {"response": "..."}     │
   │                                                  │
   │   guardrail/onyx_{input,output}.py — handlers    │
   │   guardrail/_onyx_client.py — evaluate() client  │
   └──────────────────────────────────────────────────┘
                          │
                          ▼ HTTPS
   ┌──────────────────────────────────────────────────┐
   │   Onyx AI Guard                                  │
   │   POST {api_base}/guard/evaluate/v1/{token}/simple│
   │   Auth: policy token in the URL path (not a header)│
   └──────────────────────────────────────────────────┘
```

**Two endpoints, not composite.** Each direction gets its own POST route and its own Custom Guardrail Config in the dashboard. Users can attach input-only, output-only, or both.

**SaaS round-trip per request.** Unlike NeMo (judge LLM via gateway) or Guardrails AI (local validators), every rail call that has content hits the tenant's Onyx base URL (`ONYX_API_BASE`). Budget latency accordingly (default timeout 10s). Empty user/assistant content short-circuits locally and does not call Onyx.

**One request, one mode.** Never send both `user_prompt` and `response` in the same Onyx call; never send the whole gateway `requestBody` / `responseBody`.

## Request flow

### Input (`POST /onyx-input`)

1. Extract last user text via `last_user_text(requestBody.messages)`. If `None` → `verdict=true` without calling Onyx.
2. Resolve policy token / `api_base` / `timeout` from dashboard `config` then env. Missing token → HTTP 500 (misconfiguration, not a policy decision).
3. `POST {api_base}/guard/evaluate/v1/{token}/simple` with body `{"user_prompt": "<text>"}` and header `Content-Type: application/json` only (no `Authorization` to Onyx).
4. Branch on `action` (Onyx always returns HTTP 200 for policy decisions):
   - `allow` → `ValidateGuardrailResponse(verdict=True)`
   - `block` → `verdict=False`, message from `custom_popup_message`
   - `modify` → same as block (fail-safe; see known limitation)
   - missing / empty / unrecognized `action` → HTTP 502 (do not default to allow)
5. Network / non-2xx from Onyx → HTTP 502 so the dashboard's `Fail on error` policy decides.

### Output (`POST /onyx-output`)

Identical except:

1. Short-circuit on empty `responseBody.choices` (`first_assistant_text` is `None`).
2. Body is `{"response": "<assistant text>"}` — still one mode, never both fields.
3. Block message uses `(output)` instead of `(input)`.

Whether output blocking actually fires depends on the Onyx **policy** having an Output-direction rule. Input and Output are configured independently in Onyx.

## Verdict mapping

| Onyx signal | Wrapper response |
|---|---|
| Empty messages / empty choices (local short-circuit) | `200 {"verdict": true}` |
| `action: allow` | `200 {"verdict": true}` |
| `action: block` | `200 {"verdict": false, "message": "Onyx AI Guard (input\|output): <custom_popup_message>"}` |
| `action: modify` | Same as block (fail-safe on Validate rails) |
| Missing / empty / unrecognized `action` | `502` — real error (do not default to allow) |
| Missing / invalid `ONYX_API_KEY` | `500` — real error |
| Onyx HTTP error / timeout / network | `502` — real error |

## Known limitation: `modify` / masking

v1 is **validate-only**. When Onyx returns `action: modify` (and would supply `modified_prompt` / `modified_response` for masking), these rails **block** instead of rewriting content. Applying masks in place needs a future Mutate rail that returns `MutateGuardrailResponse(verdict=True, transformed=True, result=...)` with the modified text (pattern: `integrations/lasso-security/` classifix).

## Why the call shape looks like this

`guardrail/_onyx_client.py` follows Onyx's `/simple` integration guide. Load-bearing quirks — do not "clean them up":

1. **Policy token in the URL path**, not an `Authorization` header. That path segment *is* the auth to Onyx. The only header sent is `Content-Type`.
2. **Extracted text only** (`user_prompt` *or* `response`), never the whole OpenAI body, never both fields in one request ("one request, one mode").
3. **Branch on `action`**, not on HTTP status. Onyx policy decisions are always HTTP 200; block copy lives in `custom_popup_message`.

(An earlier draft of this wrapper used a `/litellm` path with whole-body payloads and an `allowed` boolean. That does not match Onyx's current integration guide — do not revert to it.)

## Configuration surface

| Source | Keys | Purpose |
|---|---|---|
| Deploy env / TFY secret | `ONYX_API_KEY` | Policy token embedded in the evaluate URL (auth to Onyx) |
| Deploy env | `ONYX_API_BASE` | Required. Tenant AI Guard host (`https://<routing-id>.ai-guard.onyx.security`). Bare `ai-guard.onyx.security` is not routed (404s). |
| Deploy env | `ONYX_TIMEOUT` | Default timeout in seconds (default `10`) |
| Deploy env / TFY secret | `WRAPPER_API_KEY` | Bearer auth gateway → wrapper (dashboard Custom Bearer Auth) |
| Dashboard Config JSON | `credentials.apiKey` | Per-config policy-token override |
| Dashboard Config JSON | `api_base`, `timeout` | Optional per-request overrides |

Wrapper bearer auth (`WRAPPER_API_KEY`) is independent of the Onyx policy token. Three-way sync required for the wrapper key: TFY secret → pod env → dashboard Custom Bearer Auth.

Do not commit real policy tokens or tenant-specific base URLs into the repo.

## Verified

Live checks against the Onyx **test policy** (Input-direction rule only; token and base URL kept out of git):

| Case | Result |
|---|---|
| Safe prompt on `/onyx-input` | `action: allow` → wrapper `verdict: true` |
| `"fightclub"` / `"bradpitt"` / `"norton"` on `/onyx-input` | `action: block`, `custom_popup_message`: `Test blocking policy for TrueFoundry` → wrapper `verdict: false` with that message |
| Same phrases on `/onyx-output` | **Not yet verified.** Test policy has no Output-direction rule; Onyx returns `action: allow`. Output blocking needs an Output rule added in Onyx. Smoke test `test_policy_violation_output_blocks` is skipped for that reason. |

## Repo layout

```
onyx/
├── main.py                 FastAPI app: RAIL_ROUTES, bearer auth, /debug/loaded-config
├── entities.py             Pydantic models (copy of integrations/_template/entities.py)
├── guardrail/
│   ├── __init__.py
│   ├── _helpers.py         last_user_text / first_assistant_text (payload + short-circuit)
│   ├── _onyx_client.py     /simple evaluate() + resolve_settings(); keep the call shape
│   ├── onyx_input.py       input rail handler (sends user_prompt)
│   └── onyx_output.py      output rail handler (sends response)
├── deploy.py               TFY Python SDK deployment manifest
├── Dockerfile
├── requirements.txt
├── requirements-dev.txt
├── .env.example
├── tests/test_smoke.py
└── docs/
    └── DESIGN.md           (this file)
```

Per-rail files (not a single `guardrail/onyx.py`) match `_template/` and NeMo / Guardrails AI. The shared HTTP client lives in `_onyx_client.py` because both directions share the same `/simple` URL shape.

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| 401 from wrapper | Bearer token mismatch | Sync `WRAPPER_API_KEY` secret, pod env, dashboard Custom Bearer Auth |
| 500 "Onyx API key not configured" | Missing `ONYX_API_KEY` secret and no `config.credentials.apiKey` | Create secret; redeploy; or set Config JSON |
| 502 "Onyx AI Guard call failed" | Onyx outage, bad token, wrong base URL, timeout, or 200 body without usable `action` | Check Onyx status / `ONYX_API_BASE` / token; raise `ONYX_TIMEOUT`; `Fail on error` decides pass vs block |
| Input blocks but output allows the same phrase | Policy has Input rule only | Expected until an Output-direction rule is added in Onyx |
| `modify` content still blocked | Validate rails fail-safe | Expected — need a Mutate rail to apply `modified_prompt` / `modified_response` |
| Stale code after redeploy | TFY image cache | `curl /debug/loaded-config` and check `wrapper_version` |

Do not log the evaluate URL — it contains the policy token. `evaluate()` catches
httpx errors and raises `OnyxClientError` with a URL-free message (and without
chaining the raw httpx exception); rail handlers must never put raw exception
text into 502 bodies.

## Future work

1. Mutate rail that applies Onyx `modified_prompt` / `modified_response` on `action: modify` (pattern: `integrations/lasso-security/` classifix).
2. Verify output-direction blocking once the Onyx test (or production) policy has an Output rule; unskip `test_policy_violation_output_blocks`.
3. Blog draft + public-docs page (Phase 7 artifacts).
4. Promote to a native plugin in `tfy-llm-gateway` if Onyx becomes a strategic integration — HTTP mapping logic stays the same.
