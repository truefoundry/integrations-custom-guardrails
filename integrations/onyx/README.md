# Onyx Security — TrueFoundry custom guardrail

[Onyx AI Guard](https://onyx.security) speaks the TrueFoundry custom-guardrail
HTTP contract on a dedicated endpoint. Point Custom Guardrails **directly** at:

```
POST {ONYX_API_BASE}/guard/evaluate/v1/{ONYX_API_KEY}/truefoundry
```

No adapter is required. Operation: **Validate**. Auth Data: **empty** (the Guard
Token in the URL path is the auth). Enforcing Strategy: **Enforce** or
**Enforce But Ignore On Error**. Config: `{}`.

This replaces the older `/simple` wrapper flow.

> **Architecture & design notes**: see [`docs/DESIGN.md`](docs/DESIGN.md).  
> **End-user setup guide**: see [`docs/public-docs-onyx-security.md`](docs/public-docs-onyx-security.md).

## How it maps to Onyx

| Hook | Body TrueFoundry sends | Onyx evaluates |
|---|---|---|
| LLM Input | `requestBody` + `context` (+ optional `config`) | Latest user message |
| LLM Output | `requestBody` + `responseBody` + `context` | First assistant message |

Onyx always returns HTTP 200 for policy decisions:

| Onyx reply | Meaning |
|---|---|
| `{"verdict": true}` | Allow |
| `{"verdict": false, "message": "..."}` | Block (Mask/ask also map to block on Validate) |

## Env vars (local forwarder / tests only)

| Var | Purpose |
|---|---|
| `ONYX_API_KEY` | Guard Token from the AI Guard policy URL |
| `ONYX_API_BASE` | Required. Tenant host `https://<routing-id>.ai-guard.onyx.security` |
| `WRAPPER_API_KEY` | Optional. Only if you run the local FastAPI forwarder in this folder |

Do **not** commit real Guard Tokens or tenant hosts.

## Known limitation: Mask / modify

Validate cannot rewrite content. When Onyx would mask, it returns `verdict: false`
instead. Streamed responses skip output guardrails — use `"stream": false`.

## Verified

Against the Onyx TrueFoundry test policy (token and base kept out of git):

| Case | Result |
|---|---|
| Safe prompt on input | `verdict: true` |
| `bradpitt` / `fightclub` / `norton` each alone on **input** | `verdict: false` (message includes `Test blocking policy for TrueFoundry`) |
| Same keywords each alone on **output** (keyword only in `responseBody`; benign input) | `verdict: false` |

Gotcha: `/truefoundry` requires `context.user.subjectSlug`. Without it Onyx returns
`"Onyx AI Guard could not validate the request"` (still HTTP 200 + `verdict: false`).

## Optional local forwarder

This directory also contains a FastAPI app that forwards the gateway body to
`/truefoundry` and returns Onyx's verdict unchanged. Prefer the direct URL in
production.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env   # ONYX_API_KEY + ONYX_API_BASE
.venv/bin/pytest -v tests/
```

## Register in the gateway (recommended)

AI Gateway → Guardrails → New Guardrails Group → `onyx-security`:

| Name | Target | URL |
|---|---|---|
| `onyx-input` | Request | `https://<routing-id>.ai-guard.onyx.security/guard/evaluate/v1/<token>/truefoundry` |
| `onyx-output` | Response | same |

Auth Data empty. Enforce But Ignore On Error (or Enforce). Config `{}`.
