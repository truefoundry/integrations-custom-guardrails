# Onyx Security — TrueFoundry custom guardrail

FastAPI wrapper that puts [Onyx AI Guard](https://onyx.security) behind the
TrueFoundry AI Gateway custom-guardrail HTTP contract. The gateway calls this
wrapper at the `llm_input` and `llm_output` hooks; the wrapper calls Onyx
`/simple` and returns a verdict.

Validate-only (block / allow) for v1. Onyx `modify` (masking) is failed safe —
blocked — on these rails; real masking would need a future Mutate rail.

> **Architecture & design notes**: see [`docs/DESIGN.md`](docs/DESIGN.md).

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Health check (open) |
| GET | `/debug/loaded-config` | Diagnostics (bearer-gated) |
| POST | `/onyx-input` | Input rail — validate |
| POST | `/onyx-output` | Output rail — validate |

All POSTs require `Authorization: Bearer $WRAPPER_API_KEY` when that env var is set.

## How it maps to Onyx

Each rail calls:

```
POST {ONYX_API_BASE}/guard/evaluate/v1/{ONYX_API_KEY}/simple
```

The **policy token** (`ONYX_API_KEY`) in the URL path is the auth to Onyx — there
is no `Authorization` header on the Onyx call. Only `Content-Type: application/json`
is sent.

**One request, one mode.** The wrapper sends extracted text only — never the whole
gateway body, never both fields in one call:

| Rail | Body sent to Onyx |
|---|---|
| `/onyx-input` | `{"user_prompt": "<last user message>"}` |
| `/onyx-output` | `{"response": "<assistant content>"}` |

Onyx always responds HTTP 200 with an `action` of `allow`, `block`, or `modify`.
The wrapper branches on `action`:

| Onyx `action` | Wrapper response |
|---|---|
| `allow` | `{"verdict": true}` |
| `block` | `{"verdict": false, "message": "Onyx AI Guard (...): <custom_popup_message>"}` |
| `modify` | Same as block (fail-safe — validate rails cannot apply masking) |

The block message shown to callers comes from Onyx's `custom_popup_message`.

Real Onyx errors (network, non-2xx) surface as a wrapper `5xx`, so the gateway's
`Fail on error` policy — not this wrapper — decides pass vs block on an outage.

## Env vars

| Var | Purpose |
|---|---|
| `ONYX_API_KEY` | Policy token embedded in the Onyx evaluate URL (auth to Onyx) |
| `ONYX_API_BASE` | Required. Your tenant's AI Guard host (`https://<routing-id>.ai-guard.onyx.security`). Bare `https://ai-guard.onyx.security` is not routed and 404s. |
| `WRAPPER_API_KEY` | Bearer token the **gateway** presents to **this wrapper** (dashboard Custom Bearer Auth) |

Do not confuse `WRAPPER_API_KEY` with `ONYX_API_KEY` — they are different secrets
on different hops.

## Known limitation: `modify` / masking

These rails are **Validate** only. When Onyx returns `action: modify` (and would
normally supply `modified_prompt` / `modified_response`), the wrapper **blocks**
instead of rewriting content. Applying masks in place needs a future Mutate rail
that returns `MutateGuardrailResponse` with the modified text.

## Verified

Against the Onyx test policy (Input-direction rule only; policy token and base URL
kept out of the repo):

| Case | Result |
|---|---|
| Safe prompt → `/onyx-input` | `action: allow` → `verdict: true` |
| `"fightclub"` / `"bradpitt"` / `"norton"` → `/onyx-input` | `action: block`, `custom_popup_message`: `Test blocking policy for TrueFoundry` → `verdict: false` |
| Same block phrases → `/onyx-output` | **Not yet verified** — test policy has no Output-direction rule (`action: allow` today). Output blocking needs an Output rule added in Onyx. |

## Local run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env   # fill in ONYX_API_KEY, ONYX_API_BASE, WRAPPER_API_KEY
.venv/bin/uvicorn main:app --reload --port 8000
```

## Tests

```bash
.venv/bin/pytest -v tests/
```

Live Onyx cases skip unless `ONYX_API_KEY` is set, so the suite is green without
a vendor key. The output-direction block case is skipped until the Onyx test
policy gains an Output rule.

## Deploy

The wrapper is a standard Docker container. Host it on any runtime that can serve
HTTPS on a stable URL and is reachable from the TFY Gateway — ECS, Cloud Run,
Kubernetes, on-prem, or as a TrueFoundry Service via the included `deploy.py`.

**Example: deploy as a TrueFoundry Service:**

```bash
.venv/bin/pip install -U truefoundry
tfy login
.venv/bin/python deploy.py --wait
```

After every redeploy, confirm the new image is live:

```bash
curl -sS https://<host>/<path>/debug/loaded-config \
  -H "Authorization: Bearer $WRAPPER_API_KEY"
```

## Register in the gateway

AI Gateway → Guardrails → Add New Guardrails Group → `onyx-ai-guard`, then one
Custom Guardrail Config per rail:

- Name `onyx-input`, URL `https://<host>/<path>/onyx-input`, Operation `Validate`.
- Name `onyx-output`, URL `https://<host>/<path>/onyx-output`, Operation `Validate`.
- Auth Data: Custom Bearer Auth = your `WRAPPER_API_KEY`.
- Fail on error: `false` (see note below).

### `Fail on error` — resolve the repo's own contradiction

`docs/gateway-contract.md` (the single source of truth) and `CLAUDE.md` say
`false` is correct on the current gateway (post-commit `a1c551be`) because a
`200 + verdict:false` block is now distinguishable from a real outage. The
`SKILL.md` "hard rule" that says always `true` is stale — trust the contract doc.
Use `true` only if you want this security rail to fail **closed** on outages, and
verify the tenant's gateway version first.
