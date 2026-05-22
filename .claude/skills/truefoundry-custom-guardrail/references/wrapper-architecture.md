# Standard Wrapper Architecture

This is the canonical shape for a TrueFoundry custom-guardrail wrapper. Modeled after the canonical `truefoundry/custom-guardrails-template` repo. Use it verbatim. Variations cause integration failures that look like bugs in the wrong place.

## Directory layout (post-template alignment, May 2026)

```
<vendor>-guardrail-tfy/
├── .dockerignore
├── .env.example
├── .gitignore                     (includes `.guardrails/` if using Guardrails Hub)
├── Dockerfile
├── README.md
├── deploy.py                      # TFY Service deployment manifest
├── requirements.txt
├── requirements-dev.txt
├── main.py                        # FastAPI app, route registration, bearer auth, /debug
├── entities.py                    # Pydantic models (ValidateGuardrailResponse, MutateGuardrailResponse, InputGuardrailRequest, OutputGuardrailRequest, RequestContext)
├── setup.py                       # OPTIONAL: build-time installer (Guardrails Hub validators, etc.)
├── guardrail/                     # one file per rail/direction
│   ├── __init__.py
│   ├── _<vendor>_runner.py        # OPTIONAL: shared module-import singleton if init is expensive
│   ├── _helpers.py                # OPTIONAL: shared message extractors
│   ├── <rail_name>_input.py       # one function per input rail
│   └── <rail_name>_output.py      # one function per output rail
├── config/                        # OPTIONAL: vendor-specific config (Colang YAML, prompts, etc.)
├── docs/
│   ├── DESIGN.md
│   ├── blog-<vendor>.md
│   └── public-docs-<vendor>.md
└── tests/
    ├── __init__.py
    └── test_smoke.py
```

**Files live at the repo root, NOT under `app/`.** This is the convention adopted from the canonical `custom-guardrails-template`. Older wrappers using `app/main.py + app/<adapter>.py` are the pre-template layout; restructure when touching them.

## entities.py — the canonical models (copy verbatim)

```python
from typing import Any, Optional
from pydantic import BaseModel


class ValidateGuardrailResponse(BaseModel):
    """Response body for validate-operation guardrails (AI Gateway JSON contract)."""
    verdict: bool
    message: Optional[str] = None


class MutateGuardrailResponse(BaseModel):
    """Response body for mutate-operation guardrails (AI Gateway JSON contract)."""
    verdict: bool
    transformed: bool
    result: dict[str, Any]


class RequestContext(BaseModel):
    user: dict
    metadata: Optional[dict[str, str]] = None


class InputGuardrailRequest(BaseModel):
    requestBody: dict
    context: RequestContext
    config: Optional[dict] = None


class OutputGuardrailRequest(BaseModel):
    requestBody: dict
    responseBody: dict
    config: Optional[dict] = None
    context: RequestContext
```

Don't add `extra="allow"` — the schema is well-defined and the gateway uses these fields. Extra fields in incoming payloads are silently dropped, which is what we want.

## main.py — canonical shape

```python
"""FastAPI app for the <vendor> custom guardrail wrapper.

Endpoints (per-rail; one route per rail file):
    GET  /health
    POST /<rail-name>-input      # one per input rail
    POST /<rail-name>-output     # one per output rail
    GET  /debug/loaded-config    # bearer-auth gated diagnostic
"""
from __future__ import annotations
import logging, os
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request

from guardrail.<rail_name>_input import <rail_name>_input
from guardrail.<rail_name>_output import <rail_name>_output

load_dotenv()
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "info").upper())
log = logging.getLogger("<vendor>-guardrail-tfy")

WRAPPER_API_KEY = os.environ.get("WRAPPER_API_KEY", "").strip()


def require_bearer(request: Request) -> None:
    if not WRAPPER_API_KEY:
        return  # local dev only; never deploy without setting the key
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if header.split(" ", 1)[1].strip() != WRAPPER_API_KEY:
        raise HTTPException(status_code=401, detail="invalid bearer token")


app = FastAPI(title="<vendor>-guardrail-tfy", version="1.0.0")


@app.get("/")
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/debug/loaded-config", dependencies=[Depends(require_bearer)])
async def debug_loaded_config() -> dict:
    return {
        "routes": {...},      # whatever helps verify the deploy
        "wrapper_version": os.environ.get("BUILD_REF", "unknown"),
    }


app.add_api_route("/<rail-name>-input",  endpoint=<rail_name>_input,  methods=["POST"], dependencies=[Depends(require_bearer)])
app.add_api_route("/<rail-name>-output", endpoint=<rail_name>_output, methods=["POST"], dependencies=[Depends(require_bearer)])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
```

## guardrail/<rail_name>_input.py — canonical per-rail handler

```python
from entities import InputGuardrailRequest, ValidateGuardrailResponse
from guardrail._helpers import last_user_text   # OPTIONAL: shared helper
# ... import the vendor's library + validator ...


def <rail_name>_input(request: InputGuardrailRequest) -> ValidateGuardrailResponse:
    user_msg = last_user_text(request.requestBody.get("messages") or [])
    if user_msg is None:
        return ValidateGuardrailResponse(verdict=True)   # nothing to check -> pass
    try:
        # ... call the vendor; allow on success, raise on violation ...
        return ValidateGuardrailResponse(verdict=True)
    except Exception as e:
        return ValidateGuardrailResponse(
            verdict=False,
            message=f"<vendor> <rail-name> (input): {str(e)[:300]}",
        )
```

Per-rail files keep each rail isolated and easy to test. **Do NOT** compose multiple validators into one file unless they share heavy state (e.g. a model load); even then prefer one file per direction.

## Async vs sync handlers

- Sync handler functions are fine for fast local validators (Presidio, detect-secrets, etc.).
- Use `async def` when the vendor's API is itself async (NeMo's `generate_async`, vendor SaaS clients).
- FastAPI handles both transparently; mix freely.

## Response contract (the bit that breaks if you get it wrong)

| Wrapper response | Gateway interpretation (post `tfy-llm-gateway` commit `a1c551be`) |
|---|---|
| `HTTP 200` + `{"verdict": true}` | Pass (rail did not fire) |
| `HTTP 200` + `{"verdict": false, "message": "..."}` | **Block** (rail decided) — gateway propagates as `guardrail_checks_failed` |
| `HTTP 200` + `{"verdict": true, "transformed": true, "result": <body>}` | Mutate (only if dashboard `Operation: Mutate`) |
| Any non-2xx | Real error. Routed through `Fail on error` policy. |

**The wrapper's HTTP status code only carries "completed" vs "errored".** The verdict (allow vs block) lives in the JSON body. This is a behavioral change from the pre-`a1c551be` gateway, where 4xx was the block signal and `Fail on error: true` was mandatory.

After this contract: **`Fail on error: false` is the correct default** because rail-block (200 + verdict=false) and real outage (5xx) are now distinguishable.

## Build-time vendor setup (e.g. Guardrails Hub validators)

If the vendor requires a build-time installation step that needs a token (Guardrails Hub, NVIDIA NGC, etc.), use the `setup.py` pattern:

`setup.py`:
```python
import os, subprocess
TOKEN = os.getenv("GUARDRAILS_TOKEN")
if not TOKEN:
    raise RuntimeError("GUARDRAILS_TOKEN not set; pass via Docker --build-arg")

VALIDATORS = ["hub://guardrails/detect_pii", ...]

def setup_guardrails():
    subprocess.run(["guardrails", "configure", "--token", TOKEN, ...], check=True)
    for v in VALIDATORS:
        subprocess.run(["guardrails", "hub", "install", v, "--quiet"], check=True)

if __name__ == "__main__":
    setup_guardrails()
```

`Dockerfile`:
```dockerfile
ARG GUARDRAILS_TOKEN
ENV GUARDRAILS_TOKEN=${GUARDRAILS_TOKEN}
COPY setup.py .
RUN test -n "$GUARDRAILS_TOKEN" && python setup.py
```

`deploy.py`:
```python
image=Build(
    build_spec=DockerFileBuild(
        dockerfile_path="./Dockerfile",
        build_args={"GUARDRAILS_TOKEN": GUARDRAILS_TOKEN_SECRET_FQN},
    )
)
```

The token is consumed during build and is **not** present at runtime. Validators live in the image.

## Shared singletons (when vendor init is heavy)

If the vendor's runtime needs an expensive one-time setup (NeMo config load + LLM client construction, Presidio analyzer registry, HuggingFace model load), use a module-import singleton:

`guardrail/_<vendor>_runner.py`:
```python
class Runner:
    def __init__(self):
        # load config, instantiate clients, load models
        ...
    async def check_input(self, text): ...
    async def check_output(self, last_user, assistant): ...


# Instantiate at import time. If init fails, pod refuses to start — loud, correct.
runner = Runner()
```

Then `guardrail/<rail_name>_*.py` imports `from guardrail._<vendor>_runner import runner` and calls `runner.check_input(...)`.

This is the NeMo pattern — `RailsConfig.from_path` + `LLMRails(config)` is expensive, so we do it once at module import.

For lightweight vendors (Guardrails AI's per-validator Guards), just `guard = Guard().use(Validator(on_fail="exception"))` at module scope inside each `guardrail/<rail>.py` file. Each Guard is cheap and direction-specific.

## The /debug/loaded-config endpoint

Mandatory. Saves hours per deploy. Returns whatever shape lets you confirm the new image is serving the new code:

- For NeMo-style (config files): SHA-256 of each prompt, prompt task name, content head/tail, model env vars.
- For Guardrails AI-style (per-rail Guards): list of loaded routes (input + output), build ref, env summary.

How to use after every deploy:

```bash
curl -sS https://<deployed>/<path>/debug/loaded-config -H "Authorization: Bearer $WRAPPER_API_KEY" | jq
```

If the response shows old content/routes after a redeploy, the image build cache served stale layers — force a rebuild.

## Dockerfile (canonical, post-template)

```dockerfile
FROM public.ecr.aws/docker/library/python:3.11-slim
WORKDIR /app

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl bash git build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install uv && uv pip install -r requirements.txt

# Optional: build-time vendor setup (Guardrails Hub, etc.)
# ARG GUARDRAILS_TOKEN
# ENV GUARDRAILS_TOKEN=${GUARDRAILS_TOKEN}
# COPY setup.py .
# RUN test -n "$GUARDRAILS_TOKEN" && python setup.py

COPY . .

ARG BUILD_REF
ENV BUILD_REF=${BUILD_REF:-unknown}

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

`git` in apt is required because `guardrails-ai` PyPI package is quarantined; we pin to a GitHub tag in `requirements.txt`. Remove if you're not using guardrails-ai.

## tests/test_smoke.py (canonical)

Use FastAPI's `TestClient` (sync, handles lifespan via context manager). Mark live-vendor tests with `@pytest.mark.skipif(...)` so the suite runs in CI without secrets. Module-scoped client fixture so vendor init runs once per test module.

Required cases:
- `test_health` — GET `/health` returns 200.
- `test_missing_bearer_returns_401` — POST per-rail endpoint without Authorization → 401.
- `test_wrong_bearer_returns_401` — POST with wrong token → 401.
- `test_no_user_message_passes_through` — system-only history → 200 + verdict=true.
- `test_no_assistant_message_passes_through` — empty choices → 200 + verdict=true.
- `test_debug_loaded_config_*` — GET /debug returns expected route list.
- `test_<benign_case>_passes` (live) — benign input → 200 + verdict=true.
- `test_<violating_case>_blocks` (live) — violating input → 200 + verdict=false with the validator name in the message.

Assertions on the new contract:

```python
assert r.status_code == 200          # never 400 for blocks
body = r.json()
assert body["verdict"] is False      # rail decided
assert "<ValidatorName>" in body["message"]   # tells us which rail
```
