# `verra-guardrails-tfy`

[Verra](https://helloverra.com) is a managed AI governance product for regulated industries (healthcare, finance, insurance). Every request gets scanned by Verra's detection pipeline (prompt injection, jailbreak, PII, secrets, exfiltration, policy violations) and recorded in a SOC 2 / HIPAA / EU AI Act compliant audit trail.

This integration is a FastAPI proxy. It forwards every TF guardrail request to `api.helloverra.com/v1/truefoundry/*` authenticated with your Verra API key. Detection runs in Verra's backend. This service translates the TF contract so you can deploy it inside your own TF workspace.

> If you don't want to deploy a wrapper at all, you can also point TF directly at `https://api.helloverra.com/v1/truefoundry/*` with your Verra bearer in **Custom Bearer Auth**. 

## Rails shipped

Four routes covering the TF hook × operation matrix:

| Route | Op | Hook | What it does |
|---|---|---|---|
| `POST /scan-input` | validate | input | Blocks prompt injection, jailbreak, exfiltration attempts, policy violations |
| `POST /redact-input` | mutate | input | Masks PII and secrets in the prompt before it reaches the model |
| `POST /scan-output` | validate | output | Blocks secrets and policy violations in the model response |
| `POST /redact-output` | mutate | output | Masks PII and secrets in the model response |

Plus:

```
GET  /                          health check (open)
GET  /health                    health check (open)
GET  /debug/loaded-config       bearer-auth gated; lists loaded routes
```

Wire all four in TF for full coverage. If you wire only `scan-input`, secrets and PII in prompts will pass through unmasked.

## Response contract

Per [`docs/gateway-contract.md`](../../docs/gateway-contract.md):

| Status | Body | Meaning |
|---|---|---|
| `200` | `{"verdict": true}` | Pass |
| `200` | `{"verdict": false, "message": "..."}` | Block (policy deny) |
| `200` | `{"verdict": true, "transformed": <bool>, "result": <full body>}` | Mutate result (redacted or unchanged) |
| `5xx` | error JSON | Real error (Verra backend unreachable, timeout, malformed response) |

A policy deny is **never** a 4xx — it's a 2xx with `verdict:false`. Verra's backend honors this; the wrapper passes it through unchanged.

## Quick start

You need two credentials:

1. **`VERRA_KEY`** — your Verra TrueFoundry integration token. Email <support@helloverra.com> to request one (a self-serve admin UI is coming).
2. **`WRAPPER_API_KEY`** — a random string you generate, used as the bearer TF sends to *this wrapper*. Independent from `VERRA_KEY`.

```bash
cd integrations/verra
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env  # fill in WRAPPER_API_KEY and VERRA_KEY
.venv/bin/uvicorn main:app --reload --port 8000
```

Smoke test:

```bash
curl http://localhost:8000/health
curl -X POST http://localhost:8000/scan-input \
  -H "Authorization: Bearer $WRAPPER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"requestBody":{"messages":[{"role":"user","content":"hello"}]},
       "context":{"user":{"subjectId":"u1","subjectType":"user"}}}'
# -> {"verdict":true}
```

## Tests

```bash
.venv/bin/pytest -v tests/
```

Tests auto-skip live-vendor cases when `VERRA_KEY` isn't set. Set both `WRAPPER_API_KEY` and `VERRA_KEY` for a full run.

## Deploy to TrueFoundry

```bash
.venv/bin/pip install -U truefoundry
tfy login
# Fill in .env (TFY_WORKSPACE_FQN, TFY_PUBLIC_HOST, *_SECRET_FQN), then:
.venv/bin/python deploy.py --wait
```

After deploy, register each rail as a Custom Guardrail in the TFY dashboard. Use `WRAPPER_API_KEY` as the Custom Bearer Auth token. See [`docs/add-a-new-integration.md`](../../docs/add-a-new-integration.md) for the full onboarding flow.

## Config

The `config` field TF passes to the wrapper is forwarded to Verra unchanged. Recognized keys (all optional, all default to `true` for redact rails):

```jsonc
{
  "redact_pii":     true,        // input/redact + output/redact (default true)
  "redact_secrets": true,        // input/redact + output/redact (default true)
  "traceparent_metadata_key": "traceparent"  // which context.metadata key carries an inbound traceparent
}
```

Verra's org-level enforcement mode (`observe` / `govern` / `enforce`) is the source of truth and cannot be overridden by `config`.

## Architecture

```
TF customer's app ─► TF gateway ─► this wrapper ─► api.helloverra.com ─► Verra detectors
                                                         │
                                                         └─► receipts (your Verra audit trail)
```

Receipts appear in your Verra dashboard at `app.helloverra.com/admin/receipts` tagged `event_type='truefoundry_guardrail'`, with the TF user mapped to `end_user_id` and TF metadata namespaced under `findings.truefoundry.*`. Same evidence-pack workflow as direct proxy traffic.

## Support

- Verra docs: [www.helloverra.com/docs](https://www.helloverra.com/docs)
- Account / token issues: <support@helloverra.com>
- Bugs in this integration: open an issue on this repo
