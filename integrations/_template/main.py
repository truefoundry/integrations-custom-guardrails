"""FastAPI app skeleton for a new custom-guardrail integration.

Endpoints (replace placeholders):
    GET  /                            health check (open)
    GET  /health                      health check (open)
    GET  /debug/loaded-config         bearer-auth gated; diagnostic
    POST /<rail-name>-input           input validation rail
    POST /<rail-name>-output          output validation rail

Response contract: HTTP 200 with ValidateGuardrailResponse or MutateGuardrailResponse JSON.
Non-2xx is reserved for real errors. See docs/gateway-contract.md.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request

# TODO: import your per-rail handlers
# from guardrail.example_input import example_input
# from guardrail.example_output import example_output

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("<vendor>-guardrails-tfy")  # TODO: rename to match your integration

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
    title="<vendor>-guardrails-tfy",  # TODO: rename
    version="1.0.0",
)


@app.get("/")
@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


# TODO: map your rail endpoints. Each route -> per-rail function in guardrail/.
RAIL_ROUTES: dict[str, object] = {
    # "/example-input": example_input,
    # "/example-output": example_output,
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
    """Return what the running pod actually has loaded.

    Diagnostic for "did my redeploy actually replace the code". Adapt to expose
    whatever lets you verify the deployment (loaded validators, config digests,
    model names, env summary).
    """
    input_routes = [p for p in RAIL_ROUTES if p.endswith("-input")]
    output_routes = [p for p in RAIL_ROUTES if p.endswith("-output")]
    return {
        "routes": {"input": input_routes, "output": output_routes},
        "wrapper_version": os.environ.get("BUILD_REF", "unknown"),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
