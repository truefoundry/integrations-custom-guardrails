# Onyx Security — TrueFoundry custom guardrail

FastAPI wrapper that puts [Onyx AI Guard](https://onyx.security) behind the
TrueFoundry AI Gateway custom-guardrail HTTP contract. The gateway calls this
wrapper at the `llm_input` and `llm_output` hooks; the wrapper calls Onyx
`/simple` and returns a verdict.

Validate-only (block / allow) for v1. Onyx `modify` (masking) is failed safe —
blocked — on these rails; real masking would need a future Mutate rail.

> **Architecture & design notes**: see [`docs/DESIGN.md`](docs/DESIGN.md).

## Two ways to run Onyx AI Guard on the gateway

Onyx now speaks the TrueFoundry custom-guardrail contract **natively**, so there are two ways to wire it up — both return the same `{"verdict": …}` shape to the gateway; they differ only in whether you host a shim:

| Path | What you deploy | Best for |
|---|---|---|
| **Native `truefoundry` source** (recommended) | Nothing — register Onyx's evaluate URL directly as the Custom Guardrail URL | Onyx tenants with the dedicated `truefoundry` source |
| **This FastAPI wrapper** (`/simple`) | This container, on any HTTPS host reachable from the gateway | A self-hosted shim, or older Onyx tenants without the native source |

The native path is described next; the rest of this document covers the wrapper.

### Native path — no wrapper to deploy

Onyx ships a dedicated **`truefoundry`** custom-guardrail source that consumes TrueFoundry's request/response bodies directly and answers with the gateway's `{"verdict": bool, "message"?: str}` shape. There is **no wrapper container to build, host, or deploy** and **no `WRAPPER_API_KEY`** — register Onyx's evaluate endpoint as the Custom Guardrail URL and you are done.

**Custom Guardrail URL (the same URL serves both rails):**

```
POST {ONYX_API_BASE}/guard/evaluate/v1/<GUARD_TOKEN>/truefoundry
```

- `ONYX_API_BASE` — your tenant AI Guard host, `https://<routing-id>.ai-guard.onyx.security`. The bare host `https://ai-guard.onyx.security` is not routed to any tenant and 404s.
- `<GUARD_TOKEN>` — the per-policy Guard Token from the Onyx console. It sits **in the URL path** and is the only auth Onyx needs, so leave the dashboard **Custom Bearer Auth empty** for these configs.

**Contract (spoken natively by Onyx):**

| Rail | Body the gateway sends | Direction selector |
|---|---|---|
| Input (`llm_input`) | `{"requestBody": {…}, "context": {…}, "config"?: {…}}` | no `responseBody` |
| Output (`llm_output`) | the input body **plus** `"responseBody": {…}` | `responseBody` present |

| Onyx decision | Native response (always HTTP 200) |
|---|---|
| allow | `{"verdict": true}` |
| block / mask / ask | `{"verdict": false, "message": "<reason>"}` |
| unparseable / malformed body | `{"verdict": false}` — Onyx fails **closed** |

- **Validate-only.** A Mask rule is still **evaluated** — the sensitive content is detected — but because a gateway guardrail is a validate rail (it cannot hand a rewritten payload back to the gateway), a mask hit is returned as a **block** (`verdict: false`), never masked in place and **never silently allowed through**. Onyx does not return the `transformed`/`result` mutate shape on this path.
- **Always HTTP 200.** A block is a `200` with `verdict: false`, never a 4xx — matching this repo's contract.
- **Attribution.** TrueFoundry's contract carries no gateway name, so all TrueFoundry traffic rolls up under a single per-tenant **TrueFoundry Gateway** asset in the Onyx inventory.

**Register it:** AI Gateway → Guardrails → Add New Guardrails Group → `onyx-ai-guard`, then one Custom Guardrail Config per rail with **Operation `Validate`**, the URL above, **Custom Bearer Auth empty**, `Config {}`, and **Fail on error `false`** (use `true` to fail closed on an Onyx outage — Onyx already fails closed internally on a malformed body). Attach with the `X-TFY-GUARDRAILS` selector exactly like any other group.

---

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
