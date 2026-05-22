# `<vendor>-guardrails-tfy` (template)

> **This is the integration skeleton.** Copy it to start a new integration:
>
> ```bash
> cp -r integrations/_template integrations/<vendor>
> cd integrations/<vendor>
> # edit everything below per docs/add-a-new-integration.md
> ```

Replace this README with one specific to your vendor. The structure below is what every integration in this repo should follow.

## What goes here (replace per vendor)

One-paragraph summary: what the vendor does, which rails you're shipping, the gateway hook(s) you attach to (`llm_input`, `llm_output`, or both).

## Response contract

Per [`docs/gateway-contract.md`](../../docs/gateway-contract.md):

| Status | Body | Meaning |
|---|---|---|
| `200` | `{"verdict": true}` | Pass |
| `200` | `{"verdict": false, "message": "..."}` | Block |
| `5xx` | error JSON | Real error |

## Endpoints (replace per vendor)

```
GET  /health                  health check
GET  /debug/loaded-config     bearer-auth gated diagnostic
POST /<rail-name>-input       Input validation rail
POST /<rail-name>-output      Output validation rail
```

Auth: `Authorization: Bearer $WRAPPER_API_KEY`.

## Local run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env  # fill in
.venv/bin/uvicorn main:app --reload --port 8000
```

## Tests

```bash
.venv/bin/pytest -v tests/
```

## Deploy

```bash
.venv/bin/pip install -U truefoundry
tfy login
.venv/bin/python deploy.py --wait
```

See [`docs/add-a-new-integration.md`](../../docs/add-a-new-integration.md) for the full onboarding flow including dashboard registration and end-to-end verification.
