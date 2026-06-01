# guardrails-ai-tfy

Guardrails AI Hub validators as a TrueFoundry AI Gateway custom guardrail. Four local validators (DetectPII, SecretsPresent, ToxicLanguage, ProfanityFree) wrapped behind per-rail HTTP endpoints conforming to the TrueFoundry custom-guardrail contract.

> **Architecture & design notes**: see [`docs/DESIGN.md`](docs/DESIGN.md).

## Response contract

Per the TFY AI Gateway custom-guardrail contract (post `tfy-llm-gateway` commit `a1c551be`, PR #2931):

| Status | Body | Meaning |
|---|---|---|
| `200` | `{"verdict": true, "message": null}` | Pass |
| `200` | `{"verdict": false, "message": "<validator>: ..."}` | Block |
| `5xx` | error JSON | Real error (validator missing, wrapper crash) |

**Non-2xx is reserved for real errors only.** This means `Fail on error: false` is the correct default on each Custom Guardrail Config.

## Endpoints

```
GET  /                            health check
GET  /health                      health check
GET  /debug/loaded-config         bearer-auth gated; lists all loaded routes

POST /detect-pii-input            DetectPII on user input
POST /detect-pii-output           DetectPII on assistant response
POST /secrets-present-input       SecretsPresent on user input
POST /secrets-present-output      SecretsPresent on assistant response
POST /toxic-language-input        ToxicLanguage on user input
POST /toxic-language-output       ToxicLanguage on assistant response
POST /profanity-free-output       ProfanityFree on assistant response (output-only)
```

All POSTs require `Authorization: Bearer $WRAPPER_API_KEY`.

## v1 validator bundle

| Hub identifier | Direction | Engine | What it catches |
|---|---|---|---|
| `hub://guardrails/detect_pii` | input + output | Microsoft Presidio (tight allowlist — see `guardrail/_pii_entities.py`) | Email, phone, US SSN, credit card (Luhn-valid), IBAN, IP, US passport / driver license / ITIN |
| `hub://guardrails/secrets_present` | input + output | Yelp `detect-secrets` | AWS keys, OpenAI tokens, GitHub tokens, JWTs, private keys |
| `hub://guardrails/toxic_language` | input + output | Unitary Detoxify (HF), threshold 0.5 | Toxic / harassing language |
| `hub://guardrails/profanity_free` | output only | Local word list | Explicit language in assistant responses |

Validators install at **Docker build time** via `setup.py` (invoked from the Dockerfile). The Hub token is consumed during build and is not present at runtime.

## Repo layout

```
guardrails-ai-tfy/
├── main.py                       FastAPI app: routes, bearer auth, /debug
├── entities.py                   Pydantic models for the gateway contract
├── setup.py                      Hub validator installer (invoked by Dockerfile)
├── guardrail/
│   ├── __init__.py
│   ├── _helpers.py               Shared message-extraction helpers
│   ├── _pii_entities.py          Tight PII entity allowlist
│   ├── detect_pii_input.py
│   ├── detect_pii_output.py
│   ├── secrets_present_input.py
│   ├── secrets_present_output.py
│   ├── toxic_language_input.py
│   ├── toxic_language_output.py
│   └── profanity_free_output.py
├── tests/test_smoke.py           pytest, 13 cases (verdict tests auto-skip if validators absent)
├── deploy.py                     TFY Python SDK deployment manifest
├── Dockerfile                    Hub validator install at build time
├── requirements.txt              Runtime (guardrails-ai pinned to GitHub @ v0.9.3)
├── requirements-dev.txt          + pytest, httpx, requests
├── .env.example
└── docs/
    ├── DESIGN.md                 Architecture, decisions, known accuracy gaps
    ├── blog-guardrails-ai.md
    └── public-docs-guardrails-ai.md
```

The layout matches this monorepo's canonical wrapper shape (see [`integrations/_template/`](../_template/)): root-level `main.py` and `entities.py`, one file per rail under `guardrail/`, routes registered via `app.add_api_route`. The HTTP contract this conforms to is documented at [`docs/gateway-contract.md`](../../docs/gateway-contract.md).

## Local run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env   # fill GUARDRAILS_TOKEN + WRAPPER_API_KEY

# Install hub validators into THIS venv (uv inside `guardrails hub install`
# picks the closest .venv from cwd — activate explicitly to be safe).
source .venv/bin/activate
python setup.py

.venv/bin/uvicorn main:app --reload --port 8000
```

Direct smoke test:

```bash
.venv/bin/python - <<'PY'
import os, requests
from dotenv import load_dotenv
load_dotenv()
H = {"Authorization": f"Bearer {os.environ['WRAPPER_API_KEY']}", "Content-Type": "application/json"}
body = lambda c: {"requestBody":{"messages":[{"role":"user","content":c}]},"context":{"user":{"subjectId":"u1","subjectType":"user"}}}
print(requests.post("http://localhost:8000/detect-pii-input", headers=H, json=body("What is the capital of France?")).json())
print(requests.post("http://localhost:8000/detect-pii-input", headers=H, json=body("My email is jane@example.com and my SSN is 123-45-6789")).json())
PY
```

## Tests

```bash
.venv/bin/pytest -v tests/
```

13 cases: health, auth (missing/wrong bearer), short-circuits (no user/assistant message), debug endpoint, plus 7 verdict cases (benign + per-validator blocks). Verdict tests auto-skip if hub validators aren't installed.

## Docker

```bash
docker build --build-arg GUARDRAILS_TOKEN="$GUARDRAILS_TOKEN" -t guardrails-ai-tfy .
docker run --rm -p 8000:8000 --env-file .env guardrails-ai-tfy
```

## Deploy the wrapper

The wrapper is a standard Docker container. Host it on any runtime that can serve HTTPS on a stable URL reachable from your TFY Gateway — ECS / Fargate, Cloud Run, GKE / EKS / AKS, on-prem Kubernetes, or as a TrueFoundry Service via the included `deploy.py`. The example below is one option.

### A. Create secrets (example: TrueFoundry)

Dashboard → **Platform → Secrets → + Secret Group `guardrails-ai-tfy`**:

| Name | Value |
|---|---|
| `guardrails-token` | Your Hub API token from https://hub.guardrailsai.com/keys. Consumed at build time. |
| `wrapper-api-key` | A random string. Gateway sends as `Authorization: Bearer ...`. |

Paste each FQN into `.env`.

If you host elsewhere, pass `GUARDRAILS_TOKEN` as a Docker build arg (consumed at build time only) and `WRAPPER_API_KEY` as a runtime env var.

### B. Deploy (example: TrueFoundry)

```bash
pip install -U truefoundry
tfy login
python deploy.py --wait
```

The first build is slow (~5 min) because validators pull HuggingFace models. Subsequent builds use TFY's image layer cache.

For other hosts: `docker build --build-arg GUARDRAILS_TOKEN=... -t guardrails-ai-tfy .`, run with `WRAPPER_API_KEY` set, and route a public HTTPS URL to port 8000.

### C. Register as Custom Guardrails

Dashboard → **AI Gateway → Guardrails → + Add New Guardrails Group `guardrails-ai`**. Seven Custom Guardrail Configs (one per endpoint).

For each config:
- URL = the per-rail endpoint on the deployed wrapper.
- Operation = `Validate`.
- Auth Data = **Custom Bearer Auth**, value = the `wrapper-api-key` secret.
- **Fail on error**: **`false`** (with gateway commit `a1c551be`+).

### D. Apply to traffic

**Pin to a model**: Models → \<model\> → Guardrails → attach `guardrails-ai`.

**Per-request header**:

```python
extra_headers={
  "X-TFY-GUARDRAILS": json.dumps({
    "llm_input_guardrails":  ["guardrails-ai/detect-pii-input", "guardrails-ai/secrets-present-input", "guardrails-ai/toxic-language-input"],
    "llm_output_guardrails": ["guardrails-ai/detect-pii-output", "guardrails-ai/secrets-present-output", "guardrails-ai/toxic-language-output", "guardrails-ai/profanity-free-output"],
  })
}
```

## Known limitations

- **Validator accuracy is context-sensitive.** See `docs/DESIGN.md` "Known accuracy gaps" — Presidio's US_SSN recognizer and detect-secrets's tokenizer are tuned for specific framings; adversarial conversational phrasings can slip through.
- **`guardrails-ai` PyPI package is currently quarantined.** We pin to a GitHub tag in `requirements.txt`. Re-evaluate before each major deploy.
- **No mutation mode in v1.** All validators run `on_fail="exception"`. PII redaction-as-mutation is a v2 candidate.

## History

This integration originally lived in its own repo `guardrails-ai-tfy/` with a composite `app/main.py + app/guards.py` structure (sequential validators inside) returning HTTP 400 for blocks. It was restructured to per-rail endpoints + `2xx + verdict` response shape (resolved by `tfy-llm-gateway` commit `a1c551be`, PR #2931), then moved into this monorepo. See [`/docs/gateway-contract.md`](../../docs/gateway-contract.md) for the contract details.
