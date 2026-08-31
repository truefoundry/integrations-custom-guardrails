"""FastAPI app for the Onyx Security custom guardrail wrapper.

Endpoints (per-rail; one route per rail file):

    GET  /                       health check (open)
    GET  /health                 health check (open)
    POST /onyx-input             Onyx AI Guard - input validate
    POST /onyx-output            Onyx AI Guard - output validate
    GET  /debug/loaded-config    diagnostics (bearer-auth gated)

A shared bearer token gates the rail endpoints and /debug. Configure the same
token in the TrueFoundry dashboard under Custom Bearer Auth.

Response contract: per the TFY AI Gateway custom-guardrail contract (tfy-llm-gateway
commit a1c551be). Validate handlers return HTTP 200 with ValidateGuardrailResponse
JSON. Allow is {verdict: true}; block is {verdict: false, message: ...}. Non-2xx is
reserved for real errors, which the dashboard's `Fail on error` policy then routes.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request

from guardrail._onyx_client import DEFAULT_API_BASE, DEFAULT_TIMEOUT
from guardrail.onyx_input import onyx_input
from guardrail.onyx_output import onyx_output

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("onyx-guardrails-tfy")

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
    title="onyx-guardrails-tfy",
    description="Onyx AI Guard rails behind the TrueFoundry custom-guardrail contract.",
    version="1.0.0",
)


@app.get("/")
@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


# Mapping of route → function for registration and the debug endpoint.
RAIL_ROUTES: dict[str, object] = {
    "/onyx-input": onyx_input,
    "/onyx-output": onyx_output,
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

    Diagnostic for "did my redeploy actually replace the code". Exposes the
    registered routes plus a non-secret env summary (never the API key).
    """
    input_routes = [p for p in RAIL_ROUTES if p.endswith("-input")]
    output_routes = [p for p in RAIL_ROUTES if p.endswith("-output")]
    return {
        "routes": {"input": input_routes, "output": output_routes},
        "wrapper_version": os.environ.get("BUILD_REF", "unknown"),
        "onyx_api_base_env": os.environ.get("ONYX_API_BASE", DEFAULT_API_BASE),
        "default_timeout": DEFAULT_TIMEOUT,
        "onyx_api_key_configured": bool(os.environ.get("ONYX_API_KEY", "").strip()),
        "wrapper_auth_enabled": bool(WRAPPER_API_KEY),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
