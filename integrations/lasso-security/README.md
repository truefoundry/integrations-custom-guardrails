# lasso-guardrails-tfy

[Lasso Security](https://server.lasso.security) as a TrueFoundry AI Gateway custom guardrail. Forwards gateway traffic to Lasso API v3 (`classify` for validate, `classifix` for mutate).

> **Architecture & design notes**: see [`docs/DESIGN.md`](docs/DESIGN.md).  
> **End-user setup guide**: see [`docs/public-docs-lasso-security.md`](docs/public-docs-lasso-security.md).

## Response contract

Per the TFY AI Gateway custom-guardrail contract (post `tfy-llm-gateway` commit `a1c551be`):

| Status | Body | Meaning |
|---|---|---|
| `200` | `{"verdict": true}` | Pass |
| `200` | `{"verdict": false, "message": "..."}` | Block |
| `200` | `{"verdict": true, "transformed": true/false, "result": {...}}` | Mutate |
| `5xx` | error JSON | Real error |

**Non-2xx is reserved for real errors only.** Set **Fail on error: false** on each Custom Guardrail Config in TrueFoundry.

## Endpoints

```
GET  /                       health check
GET  /health                 health check
GET  /debug/runtime-config   bearer-auth gated — post-deploy verification
POST /lasso-classify         Lasso classify — input validate
POST /lasso-classify-output  Lasso classify — output validate
POST /lasso-classifix        Lasso classifix — input mutate (PII masking)
POST /lasso-classifix-output Lasso classifix — output mutate
```

All POSTs require `Authorization: Bearer $WRAPPER_API_KEY` when set.

## v1 rail bundle

Four rails mapping to Lasso API v3:

| Rail | Lasso endpoint | Gateway operation |
|---|---|---|
| Input validate | `POST /classify` (`messageType=PROMPT`) | Validate |
| Output validate | `POST /classify` (`messageType=COMPLETION`) | Validate |
| Input mutate | `POST /classifix` (`messageType=PROMPT`) | Mutate |
| Output mutate | `POST /classifix` (`messageType=COMPLETION`) | Mutate |

Policy deputies and BLOCK/WARN thresholds are configured in the Lasso console. The wrapper only forwards messages and maps findings to verdicts.

## Repo layout

```
lasso-guardrails-tfy/
├── main.py                 FastAPI app: routes, bearer auth, /debug/runtime-config
├── entities.py             Pydantic models (validate + mutate response types)
├── guardrail/
│   ├── __init__.py
│   └── lasso.py            Lasso HTTP client and four rail handlers
├── deploy.py               TFY Python SDK deployment manifest
├── Dockerfile
├── requirements.txt
├── .env.example
└── docs/
    ├── DESIGN.md
    └── public-docs-lasso-security.md
```

The layout follows this monorepo's canonical wrapper shape (see [`integrations/_template/`](../_template/)). The HTTP contract is documented at [`docs/gateway-contract.md`](../../docs/gateway-contract.md).

## Local run

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt   # Windows
# .venv/bin/pip install -r requirements.txt       # macOS/Linux
cp .env.example .env                              # set LASSO_API_KEY, WRAPPER_API_KEY
.venv\Scripts\uvicorn main:app --reload --port 8000
```

## Docker

```bash
docker build -t lasso-guardrails-tfy .
docker run --rm -p 8000:8000 --env-file .env lasso-guardrails-tfy
```

## Deploy to TrueFoundry

1. Create secret group `lasso-guardrails-tfy` with `lasso-api-key` and `wrapper-api-key`
2. Fill deploy fields in `.env`, then:

```bash
pip install -U truefoundry
tfy login
python deploy.py --wait
```

3. Register four Custom Guardrail Configs in **AI Gateway → Guardrails**:

| Name | Operation | URL |
|---|---|---|
| `lasso-classify-input` | Validate | `https://<host>/<path>/lasso-classify` |
| `lasso-classify-output` | Validate | `…/lasso-classify-output` |
| `lasso-classifix-input` | Mutate | `…/lasso-classifix` |
| `lasso-classifix-output` | Mutate | `…/lasso-classifix-output` |

Auth: **Custom Bearer Auth** with your `wrapper-api-key` value.  
Config: `{}` (Lasso key comes from deploy secret, or pass via `config.credentials.apiKey`).

**Pin to a model**: Models → \<model\> → Guardrails → attach your Lasso group.

```json
{
  "llm_input_guardrails":  ["lasso-security/lasso-classify-input"],
  "llm_output_guardrails": ["lasso-security/lasso-classify-output"]
}
```

## References

- [TrueFoundry custom guardrails](https://docs.truefoundry.com/gateway/custom-guardrails)
- [Lasso Security](https://server.lasso.security)
- Monorepo overview: [`../../README.md`](../../README.md)
