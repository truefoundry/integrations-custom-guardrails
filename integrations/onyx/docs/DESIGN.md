# Design notes

How and why the Onyx Security integration is shaped the way it is. Read [`../README.md`](../README.md) for the quickstart; this doc is for "I'm about to change something non-trivial and need the context."

## Problem

The TrueFoundry AI Gateway plugs in custom guardrails over a simple HTTP contract (post `tfy-llm-gateway` commit `a1c551be`):

```
POST <custom-guardrail-url>
{ requestBody, [responseBody], context, config }
  -> 200 {"verdict": true}                     => pass through
  -> 200 {"verdict": false, "message": "..."}  => block
  -> 5xx error                                 => guardrail failure (per enforcing strategy)
```

**[Onyx AI Guard](https://onyx.security)** now exposes a dedicated `/truefoundry` evaluate path that **already speaks that contract**. The Guard Token lives in the URL path. No Authorization header, no adapter, no FastAPI wrapper is required between the gateway and Onyx.

## Why direct (vs. the old /simple wrapper)

| Path | What | Verdict |
|---|---|---|
| **A. Direct Custom Guardrail → `/truefoundry`** | Dashboard URL points at Onyx. Auth Data empty. | **Chosen.** Matches Onyx's TrueFoundry integration guide (Sep 2026). |
| B. FastAPI `/simple` wrapper | Extract text → `user_prompt`/`response` → map `action` → `verdict`. | **Retired.** Extra hop; different request/response shape. |
| C. Native plugin in `tfy-llm-gateway` | Defer until demand justifies it. | Optional later. |

An optional thin forwarder remains under `integrations/onyx/` for local pytest and debugging. It POSTs the same TrueFoundry body to `/truefoundry` and returns Onyx's `verdict`/`message` unchanged. Production should call Onyx directly.

## Architecture

```
   ┌──────────────────────────────────────────────────┐
   │           TrueFoundry AI Gateway                 │
   │   (input hook → LLM → output hook)               │
   └──────────────────────────────────────────────────┘
         │                                      │
         │ POST …/truefoundry                   │ POST …/truefoundry
         │ (onyx-input config)                  │ (onyx-output config)
         ▼                                      ▼
   ┌──────────────────────────────────────────────────┐
   │   Onyx AI Guard                                  │
   │   POST {tenant}/guard/evaluate/v1/{token}/truefoundry │
   │   Auth: Guard Token in URL path                  │
   │   Returns: {"verdict": bool, "message"?: str}    │
   └──────────────────────────────────────────────────┘
```

Both dashboard configs use the **same** Onyx URL. Presence of `responseBody` selects output evaluation.

## Request flow

### Input

1. Gateway POSTs `{requestBody, context, config}` (no `responseBody`).
2. Onyx evaluates the latest user message (text parts of multimodal messages joined).
3. Empty / no user text → allow. Block / Mask / ask → `verdict: false` with message.
4. Gateway applies Enforcing Strategy (Enforce, Enforce But Ignore On Error, or Audit).

### Output

1. Gateway POSTs `{requestBody, responseBody, context, config}` after the model answers.
2. Onyx evaluates the first assistant message.
3. Requires `"stream": false` — streamed responses skip output guardrails.
4. Keep blocked keywords out of the **input** when testing output so the request reaches the model.

## Verdict mapping

| Onyx decision | Reply to TrueFoundry |
|---|---|
| allow | `{"verdict": true}` |
| block | `{"verdict": false, "message": "<reason>"}` |
| modify (Mask) or ask | `{"verdict": false, "message": "<reason>"}` |

Unparseable requests also return `verdict: false` on HTTP 200.

## Configuration surface

| Source | Keys | Purpose |
|---|---|---|
| Dashboard Custom Guardrail URL | tenant host + Guard Token + `/truefoundry` | Production path |
| Dashboard | Auth Data empty; Config `{}` | Token is in the path |
| Dashboard | Enforcing Strategy | Block vs audit vs fail-open on errors |
| Local forwarder env | `ONYX_API_KEY`, `ONYX_API_BASE` | Smoke tests only |

Do not commit real Guard Tokens or tenant-specific base URLs.

## Verified

Live checks against the Onyx TrueFoundry test policy (Input + Output keyword rules; credentials kept out of git):

| Case | Result |
|---|---|
| Safe prompt on input | `verdict: true` |
| `bradpitt`, `fightclub`, `norton` each alone on input | `verdict: false` (`Test blocking policy for TrueFoundry`) |
| Same keywords each alone on output (keyword only in assistant content) | `verdict: false` |

**Gotcha:** `context.user.subjectSlug` is required. Missing it yields
`"Onyx AI Guard could not validate the request"` (HTTP 200 + `verdict: false`), which
looks like a policy block but is a payload validation failure. TrueFoundry normally
supplies `subjectSlug`; include it in any manual curl / test fixtures.

## Repo layout

```
onyx/
├── main.py                 Optional FastAPI forwarder (local / tests)
├── entities.py             TFY Pydantic models
├── guardrail/
│   ├── _onyx_client.py     POST …/truefoundry; parse verdict
│   ├── onyx_input.py       Forward input payload
│   └── onyx_output.py      Forward output payload
├── tests/test_smoke.py     Local + live /truefoundry cases
├── docs/
│   ├── DESIGN.md           (this file)
│   └── public-docs-onyx-security.md
└── README.md
```

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Guardrail non-200 | Wrong host / token / inactive policy | Fix AI Guard URL; check outbound HTTPS |
| Input blocks, output allows | No Output rule, missing output config, or streaming | Enable Output scan; `"stream": false` |
| Mask content blocked | Validate cannot rewrite | Expected |
| Token leaked in client errors | Gateway includes URL in `guardrail_checks` | Redact logs; rotate Guard Token |

Do not log the evaluate URL — it contains the Guard Token. The optional forwarder's
`evaluate()` raises `OnyxClientError` with a URL-free message (no chained httpx cause).

## Future work

1. Mutate rail if TrueFoundry + Onyx add in-place masking on this path.
2. Native plugin in `tfy-llm-gateway` if volume justifies it.
3. Remove the optional forwarder once all consumers use the direct URL.
