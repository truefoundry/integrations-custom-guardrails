# HiddenLayer AISec guardrails for TrueFoundry

FastAPI wrapper that connects the [TrueFoundry AI Gateway](https://docs.truefoundry.com) custom-guardrail contract to [HiddenLayer Runtime Security](https://hiddenlayer.com) (`POST /detection/v1/interactions`).

## Rails

| Endpoint | Hook | Dashboard Operation | HiddenLayer phase |
|---|---|---|---|
| `/validate-input` | `llm_input` | Validate | `input` only — blocks on `Block` / `Redact` |
| `/validate-output` | `llm_output` | Validate | `output` only — blocks on `Block` / `Redact` |
| `/redact-input` | `llm_input` | Mutate | `input` — applies `modified_data.input` on `Redact` |
| `/redact-output` | `llm_output` | Mutate | `output` — applies `modified_data.output` on `Redact` |

`Allow` and `Alert` actions pass through on all rails. Policy enforcement modes (Ignore / Alert / Alert-and-Block) are configured in the HiddenLayer AISec console per Project.

### API mapping (v1/interactions)

- **Input rails** send `{ metadata, input }` only (pre-model scan).
- **Output rails** send `{ metadata, output }` only (post-model scan).
- **Auth**: OAuth2 client-credentials → cached Bearer JWT, refreshed on `401`.
- **Headers**: `HL-Project-Id`, `HL-Runtime-Session-Id` (session grouping).
- **Enforcement**: driven solely by `evaluation.action` — never inferred from `policy.block_*`.

## Quick start

```bash
cd integrations/hiddenlayer
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env   # fill HIDDENLAYER_CLIENT_ID, HIDDENLAYER_CLIENT_SECRET, WRAPPER_API_KEY
.venv/bin/uvicorn main:app --reload --port 8000
```

## Configuration

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `HIDDENLAYER_CLIENT_ID` | yes | OAuth2 client ID from AISec Console |
| `HIDDENLAYER_CLIENT_SECRET` | yes | OAuth2 client secret |
| `HIDDENLAYER_PROJECT_ID` | no | `HL-Project-Id` header (project ID or alias) |
| `HIDDENLAYER_REGION` | no | `us` (default) or `eu` |
| `WRAPPER_API_KEY` | yes (prod) | Bearer token the TFY gateway sends |

### Dashboard `config` JSON (per guardrail config)

```json
{
  "credentials": {
    "clientId": "...",
    "clientSecret": "..."
  },
  "projectId": "default-project",
  "region": "us",
  "requesterId": "user-123",
  "sessionId": "sess-abc",
  "fail_open_on_unavailable": false
}
```

Config values override environment defaults at request time.

## TrueFoundry dashboard registration

1. **Guardrails → + Add New Guardrails Group** — name: `hiddenlayer-guardrails`
2. Add one **Custom Guardrail Config** per rail URL
3. **Auth Data:** Custom Bearer Auth = your `WRAPPER_API_KEY`
4. **Fail on error:** `false`
5. Attach via `X-TFY-GUARDRAILS`:

```json
{
  "llm_input_guardrails": ["hiddenlayer-guardrails/validate-input"],
  "llm_output_guardrails": ["hiddenlayer-guardrails/validate-output"]
}
```

## Deploy

```bash
pip install -U truefoundry
tfy login
python deploy.py --wait
```

## Tests

```bash
.venv/bin/pytest tests/test_smoke.py -v
```

Live tests run when `HIDDENLAYER_CLIENT_ID` and `HIDDENLAYER_CLIENT_SECRET` are set.
