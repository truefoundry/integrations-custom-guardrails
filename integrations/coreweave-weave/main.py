"""FastAPI app for the CoreWeave Weave guardrails wrapper.

Endpoints:

    GET  /                              health check (open)
    GET  /health                        health check (open)
    POST /toxicity-input                WeaveToxicityScorerV1 on user input (validate; blocks on toxicity)
    POST /toxicity-output               WeaveToxicityScorerV1 on assistant response (validate; blocks on toxicity)
    POST /toxicity-input-mutate         Same scorer, masks the user message on detect (mutate)
    POST /toxicity-output-mutate        Same scorer, replaces the assistant response on detect (mutate)
    GET  /debug/loaded-config           bearer-auth gated; lists loaded scorer + thresholds

A shared bearer token gates rail endpoints and /debug. Configure the same token
in the TrueFoundry dashboard under Custom Bearer Auth.

Response contract (per tfy-llm-gateway commit a1c551be):
    Allow -> HTTP 200 + {"verdict": true}
    Block -> HTTP 200 + {"verdict": false, "message": "..."}
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request

from guardrail.toxicity_input import toxicity_input
from guardrail.toxicity_input_mutate import toxicity_input_mutate
from guardrail.toxicity_output import toxicity_output
from guardrail.toxicity_output_mutate import toxicity_output_mutate
from guardrail._weave_runner import (
    DEFAULT_AGGREGATION,
    DEFAULT_CATEGORY_THRESHOLD,
    DEFAULT_TOTAL_THRESHOLD,
    scorer,
)

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("coreweave-weave-guardrails-tfy")

WRAPPER_API_KEY = os.environ.get("WRAPPER_API_KEY", "").strip()


def require_bearer(request: Request) -> None:
    if not WRAPPER_API_KEY:
        return
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if header.split(" ", 1)[1].strip() != WRAPPER_API_KEY:
        raise HTTPException(status_code=401, detail="invalid bearer token")


app = FastAPI(
    title="coreweave-weave-guardrails-tfy",
    description="CoreWeave Weave scorers behind the TrueFoundry custom-guardrail contract.",
    version="1.0.0",
)


@app.get("/")
@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


RAIL_ROUTES: dict[str, object] = {
    "/toxicity-input": toxicity_input,
    "/toxicity-output": toxicity_output,
    "/toxicity-input-mutate": toxicity_input_mutate,
    "/toxicity-output-mutate": toxicity_output_mutate,
}

for path, fn in RAIL_ROUTES.items():
    app.add_api_route(
        path,
        endpoint=fn,
        methods=["POST"],
        dependencies=[Depends(require_bearer)],
    )


@app.get("/debug/loaded-config", dependencies=[Depends(require_bearer)])
async def debug_loaded_config() -> dict:
    """Diagnostic: confirm which scorer + thresholds are loaded on this pod."""
    validate_input = [p for p in RAIL_ROUTES if p.endswith("-input")]
    validate_output = [p for p in RAIL_ROUTES if p.endswith("-output")]
    mutate_input = [p for p in RAIL_ROUTES if p.endswith("-input-mutate")]
    mutate_output = [p for p in RAIL_ROUTES if p.endswith("-output-mutate")]
    return {
        "routes": {
            "validate": {"input": validate_input, "output": validate_output},
            "mutate": {"input": mutate_input, "output": mutate_output},
        },
        "scorer": {
            "class": type(scorer).__name__,
            "device": scorer.device,
            "max_tokens": scorer.max_tokens,
            "overlap": scorer.overlap,
        },
        "defaults": {
            "total_threshold": DEFAULT_TOTAL_THRESHOLD,
            "category_threshold": DEFAULT_CATEGORY_THRESHOLD,
            "aggregation_method": DEFAULT_AGGREGATION,
        },
        "wrapper_version": os.environ.get("BUILD_REF", "unknown"),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
