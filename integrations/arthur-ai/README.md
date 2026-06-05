# `arthur-guardrails-tfy`

[Arthur GenAI Engine](https://platform.arthur.ai) stateless validation behind the TrueFoundry custom-guardrail contract. The wrapper forwards each TF guardrail call to `POST /api/v2/validate` on Arthur's platform and maps rule results to `verdict: true/false`.

Arthur is **validate-only** — it reports what failed but does not return redacted or rewritten text. Use **Operation: Validate** for both rails in the TF dashboard.

## Rails shipped

| Route | Hook | What it does |
|---|---|---|
| `POST /validate-input` | input | Runs Arthur checks against the latest user message (`prompt`) |
| `POST /validate-output` | output | Runs Arthur checks against the assistant completion (`response`) |

Plus:

```
GET  /                          health check (open)
GET  /health                    health check (open)
GET  /debug/loaded-config       bearer-auth gated diagnostics
```

## Response contract

Per [`docs/gateway-contract.md`](../../docs/gateway-contract.md):

| Status | Body | Meaning |
|---|---|---|
| `200` | `{"verdict": true}` | Pass |
| `200` | `{"verdict": false, "message": "..."}` | Block (policy deny) |
| `5xx` | error JSON | Real error (Arthur unreachable, bad key, misconfiguration) |

Policy denies are **never** HTTP 4xx — always `200` + `verdict: false`.

## Quick start

You need two credentials:

1. **`ARTHUR_API_KEY`** — Bearer token from the Arthur platform.
2. **`WRAPPER_API_KEY`** — random string the TF gateway sends to this wrapper (independent from Arthur).

```bash
cd integrations/arthur-ai
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env   # fill in WRAPPER_API_KEY and ARTHUR_API_KEY
.venv/bin/uvicorn main:app --reload --port 8000
```

Smoke test:

```bash
curl http://localhost:8000/health
curl -X POST http://localhost:8000/validate-input \
  -H "Authorization: Bearer $WRAPPER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "requestBody": {"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]},
    "context": {"user": {"subjectId": "u1", "subjectType": "user"}},
    "config": {
      "checks": [
        {"name": "prompt-injection-check", "type": "PromptInjectionRule",
         "apply_to_prompt": true, "apply_to_response": false},
        {"name": "toxicity-check", "type": "ToxicityRule",
         "apply_to_prompt": true, "apply_to_response": false,
         "config": {"threshold": 0.5}}
      ]
    }
  }'
# -> {"verdict": true}
```

## Dashboard config

Register **two** Custom Guardrail Configs (input + output). Set **Operation** to **Validate**.

The `config` JSON in the dashboard drives Arthur's `checks` array. Example for input:

```json
{
  "checks": [
    {
      "name": "prompt-injection-check",
      "type": "PromptInjectionRule",
      "apply_to_prompt": true,
      "apply_to_response": false
    },
    {
      "name": "toxicity-check",
      "type": "ToxicityRule",
      "apply_to_prompt": true,
      "apply_to_response": false,
      "config": {"threshold": 0.5}
    }
  ],
  "fail_closed_on_unavailable": false
}
```

For output rails, set `apply_to_response: true` on the checks you want to run against the model completion. For `ModelHallucinationRuleV2`, also set top-level `context` (grounding text) or rely on system messages from the original request.

Optional config keys:

| Key | Purpose |
|---|---|
| `credentials.apiKey` | Override `ARTHUR_API_KEY` env var |
| `api_base` | Override Arthur API host (default `https://engine.platform.arthur.ai`) |
| `timeout` | Request timeout in seconds (default 30) |
| `context` / `grounding_context` | Grounding text for hallucination checks |
| `fail_closed_on_unavailable` | If `true`, block when Arthur returns Skipped/Unavailable (default `false`) |

## Verdict mapping

| Arthur `result` | Wrapper behavior |
|---|---|
| `Pass` | `verdict: true` |
| `Fail` | `verdict: false` with check names in `message` |
| `Skipped`, `Unavailable`, `Partially Unavailable`, `Model Not Available` | Allow by default; block if `fail_closed_on_unavailable: true` |

## Tests

```bash
.venv/bin/pytest -v tests/
```

Live-vendor tests auto-skip when `ARTHUR_API_KEY` is unset.

## Deploy to TrueFoundry

```bash
.venv/bin/pip install -U truefoundry
tfy login
# Fill in .env (TFY_WORKSPACE_FQN, TFY_PUBLIC_HOST, *_SECRET_FQN), then:
.venv/bin/python deploy.py --wait
```

After deploy, register `/validate-input` and `/validate-output` as Custom Guardrail Configs. Use `WRAPPER_API_KEY` as Custom Bearer Auth.

## Architecture

```
TF customer's app ─► TF gateway ─► this wrapper ─► engine.platform.arthur.ai/api/v2/validate
```

See [`docs/add-a-new-integration.md`](../../docs/add-a-new-integration.md) for the full onboarding flow.
