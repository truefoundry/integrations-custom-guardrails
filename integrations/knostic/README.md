# knostic-guardrails-tfy

[Knostic](https://www.knostic.ai) Prompt Gateway and AI guardrails as a TrueFoundry AI Gateway custom guardrail. Forwards gateway traffic to your Knostic tenant API for need-to-know enforcement, prompt-injection defense, and inline PII/secrets masking.

> **Architecture & design notes**: see [`docs/DESIGN.md`](docs/DESIGN.md).  
> **End-user setup guide**: see [`docs/public-docs-knostic.md`](docs/public-docs-knostic.md).

## Prerequisites

Knostic's runtime API is provisioned per enterprise tenant. Contact [Knostic](https://www.knostic.ai) for API credentials, base URL, and path names. The defaults in `.env.example` are placeholders until your tenant details are confirmed.

## Response contract

Per the TFY AI Gateway custom-guardrail contract (post `tfy-llm-gateway` commit `a1c551be`):

| Status | Body | Meaning |
|---|---|---|
| `200` | `{"verdict": true}` | Pass |
| `200` | `{"verdict": false, "message": "..."}` | Block |
| `200` | `{"verdict": true, "transformed": true/false, "result": {...}}` | Mutate |
| `5xx` | error JSON | Real error |

**Non-2xx is reserved for real errors only.** Set **Fail on error: false** on each Custom Guardrail Config in TrueFoundry (post `a1c551be`).

## Endpoints

```
GET  /                              health check
GET  /health                        health check
GET  /debug/loaded-config           bearer-auth gated — post-deploy verification
POST /knostic-prompt-inspect-input  Knostic inspect — input validate
POST /knostic-prompt-inspect-output Knostic inspect — output validate
POST /knostic-prompt-sanitize-input Knostic sanitize — input mutate
POST /knostic-prompt-sanitize-output Knostic sanitize — output mutate
```

All POSTs require `Authorization: Bearer $WRAPPER_API_KEY` when set.

## v1 rail bundle

Four rails mapping to Knostic Prompt Gateway capabilities:

| Rail | Knostic operation | Gateway operation |
|---|---|---|
| Input validate | `inspect` (`messageType=PROMPT`) | Validate |
| Output validate | `inspect` (`messageType=COMPLETION`) | Validate |
| Input mutate | `sanitize` (`messageType=PROMPT`) | Mutate |
| Output mutate | `sanitize` (`messageType=COMPLETION`) | Mutate |

Policies (need-to-know, injection sensitivity, DLP rules) are configured in the Knostic console. The wrapper forwards messages, user/session context, and optional `policyId`.

## Repo layout

```
knostic-guardrails-tfy/
├── main.py
├── entities.py
├── guardrail/
│   ├── _helpers.py
│   └── _knostic_client.py
├── deploy.py
├── Dockerfile
├── requirements.txt
├── knostic_smoke.ipynb
└── docs/
```

## Local run

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env   # KNOSTIC_API_KEY, WRAPPER_API_KEY
.venv/bin/uvicorn main:app --reload --port 8000
```

## Tests

```bash
.venv/bin/pytest -v tests/
```

Mocked tests run without Knostic credentials. For live API tests:

```bash
export KNOSTIC_API_KEY=...
export KNOSTIC_LIVE_TESTS=1
.venv/bin/pytest -v tests/test_smoke.py::test_live_benign_input
```

## Deploy

See [`deploy.py`](deploy.py) and `.env.example` for TrueFoundry Service deployment. After deploy, register four Custom Guardrail Configs in **AI Gateway → Guardrails** with URLs pointing at each rail path.

Example selector header:

```json
{
  "llm_input_guardrails": ["knostic-prompt-gateway/knostic-prompt-inspect-input"],
  "llm_output_guardrails": ["knostic-prompt-gateway/knostic-prompt-inspect-output"]
}
```

Add sanitize rails with `Operation: Mutate` when you want inline redaction instead of hard blocks.
