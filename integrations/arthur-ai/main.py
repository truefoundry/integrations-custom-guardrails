"""FastAPI app for the Arthur AI custom-guardrail wrapper.

Arthur GenAI Engine is validate-only (no mutation). This service translates
TrueFoundry guardrail requests to POST /api/v2/validate and maps Arthur rule
results back to ValidateGuardrailResponse.

Endpoints:
    GET  /                       health check (open)
    GET  /health                 health check (open)
    POST /validate-input         validate input (llm_input hook)
    POST /validate-output        validate output (llm_output hook)
    GET  /debug/loaded-config    bearer-auth gated diagnostics

Response contract:
    Allow  -> HTTP 200 + {"verdict": true}
    Block  -> HTTP 200 + {"verdict": false, "message": "..."}
    Error  -> HTTP 4xx/5xx (infra / misconfiguration only)
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from guardrail._arthur_client import DEFAULT_API_BASE, DEFAULT_TIMEOUT
from guardrail._defaults import DEFAULT_INPUT_CHECKS, DEFAULT_OUTPUT_CHECKS
from guardrail.validate import validate_input, validate_output

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("arthur-guardrails-tfy")

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
    title="arthur-guardrails-tfy",
    description="Arthur GenAI Engine validate rails behind the TrueFoundry custom-guardrail contract.",
    version="1.0.0",
)


@app.get("/")
@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


RAIL_ROUTES: dict[str, object] = {
    "/validate-input": validate_input,
    "/validate-output": validate_output,
}

for path, handler in RAIL_ROUTES.items():
    app.add_api_route(
        path,
        endpoint=handler,
        methods=["POST"],
        status_code=200,
        dependencies=[Depends(require_bearer)],
    )


@app.get("/debug/loaded-config", dependencies=[Depends(require_bearer)])
async def debug_loaded_config() -> dict:
    return {
        "routes": {
            "input": [p for p in RAIL_ROUTES if p.endswith("-input")],
            "output": [p for p in RAIL_ROUTES if p.endswith("-output")],
        },
        "arthur_api_base": os.environ.get("ARTHUR_API_BASE", DEFAULT_API_BASE),
        "default_timeout": DEFAULT_TIMEOUT,
        "arthur_api_key_configured": bool(os.environ.get("ARTHUR_API_KEY", "").strip()),
        "wrapper_auth_enabled": bool(WRAPPER_API_KEY),
        "wrapper_version": os.environ.get("BUILD_REF", "unknown"),
        "default_checks": {
            "input": DEFAULT_INPUT_CHECKS,
            "output": DEFAULT_OUTPUT_CHECKS,
        },
    }


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": "Guardrail server error", "detail": exc.detail},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request, exc: Exception):
    if isinstance(exc, HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": "Guardrail server error", "detail": exc.detail},
        )
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "detail": str(exc)},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
