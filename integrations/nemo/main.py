"""FastAPI app for the NeMo Guardrails custom guardrail wrapper.

Endpoints (per-rail; matches template convention of one route per rail file):

    GET  /                     health check (open)
    GET  /health               health check (open)
    POST /self-check-input     NeMo self_check_input rail
    POST /self-check-output    NeMo self_check_output rail
    GET  /debug/loaded-config  diagnostics (bearer-auth gated)

A shared bearer token gates the rail endpoints and /debug. Configure the same
token in the TrueFoundry dashboard under Custom Bearer Auth.

Response contract: per the TFY AI Gateway custom-guardrail contract (with
commit a1c551be on tfy-llm-gateway), rail handlers return HTTP 200 with
ValidateGuardrailResponse JSON. Allow is `{verdict: true}`; block is
`{verdict: false, message: ...}`. Non-2xx is reserved for real errors.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request

from guardrail.self_check_input import self_check_input
from guardrail.self_check_output import self_check_output

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("nemo-guardrails-tfy")

WRAPPER_API_KEY = os.environ.get("WRAPPER_API_KEY", "").strip()


def require_bearer(request: Request) -> None:
    """Bearer-auth dependency. No key configured -> auth disabled (local dev only)."""
    if not WRAPPER_API_KEY:
        return
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if header.split(" ", 1)[1].strip() != WRAPPER_API_KEY:
        raise HTTPException(status_code=401, detail="invalid bearer token")


app = FastAPI(
    title="nemo-guardrails-tfy",
    description="NeMo Guardrails self_check rails behind the TrueFoundry custom-guardrail contract.",
    version="2.0.0",
)


@app.get("/")
@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/debug/loaded-config", dependencies=[Depends(require_bearer)])
async def debug_loaded_config() -> dict:
    """Return what the running pod actually has loaded.

    Diagnostic for "did my redeploy actually replace the prompts/model".
    For each prompt we return the first/last 80 chars + a sha256 digest, so
    the wire payload stays small while the digest detects any drift.
    """
    import hashlib

    from guardrail._nemo_runner import runner

    cfg = runner._config
    prompts = []
    for p in (cfg.prompts or []):
        content = getattr(p, "content", None) or ""
        digest = hashlib.sha256(content.encode()).hexdigest()
        prompts.append({
            "task": getattr(p, "task", None),
            "content_len": len(content),
            "content_sha256": digest,
            "content_head": content[:80],
            "content_tail": content[-80:],
            "output_parser": getattr(p, "output_parser", None),
        })

    models = []
    for m in (cfg.models or []):
        models.append({
            "type": getattr(m, "type", None),
            "engine": getattr(m, "engine", None),
            "model": getattr(m, "model", None),
        })

    return {
        "wrapper_version": os.environ.get("BUILD_REF", "unknown"),
        "prompts": prompts,
        "models": models,
        "judge_model_env": os.environ.get("JUDGE_MODEL"),
        "tfy_base_url_env": os.environ.get("TFY_BASE_URL"),
    }


# Per-rail routes. Template convention: one POST per rail, registered via
# `app.add_api_route`, function lives in `guardrail/<rail_name>.py`.
app.add_api_route(
    "/self-check-input",
    endpoint=self_check_input,
    methods=["POST"],
    dependencies=[Depends(require_bearer)],
)
app.add_api_route(
    "/self-check-output",
    endpoint=self_check_output,
    methods=["POST"],
    dependencies=[Depends(require_bearer)],
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
