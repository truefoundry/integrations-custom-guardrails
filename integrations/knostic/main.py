"""FastAPI app for the Knostic custom guardrail wrapper.

Endpoints (per-rail; one route per guardrail handler):

    GET  /                            health check (open)
    GET  /health                      health check (open)
    POST /knostic-prompt-inspect-input    Knostic inspect — input validate
    POST /knostic-prompt-inspect-output   Knostic inspect — output validate
    POST /knostic-prompt-sanitize-input   Knostic sanitize — input mutate
    POST /knostic-prompt-sanitize-output  Knostic sanitize — output mutate
    GET  /debug/loaded-config         diagnostics (bearer-auth gated)

Response contract: HTTP 200 + ValidateGuardrailResponse or MutateGuardrailResponse JSON.
Non-2xx is reserved for real errors. See docs/gateway-contract.md.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from guardrail._knostic_client import (
    DEFAULT_API_BASE,
    DEFAULT_INSPECT_PATH,
    DEFAULT_SANITIZE_PATH,
    DEFAULT_TIMEOUT,
    knostic_prompt_inspect_input,
    knostic_prompt_inspect_output,
    knostic_prompt_sanitize_input,
    knostic_prompt_sanitize_output,
)

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("knostic-guardrails-tfy")

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
    title="knostic-guardrails-tfy",
    description=(
        "Knostic Prompt Gateway / AI guardrails behind the TrueFoundry custom-guardrail contract."
    ),
    version="1.0.0",
)

RAIL_ROUTES: dict[str, object] = {
    "/knostic-prompt-inspect-input": knostic_prompt_inspect_input,
    "/knostic-prompt-inspect-output": knostic_prompt_inspect_output,
    "/knostic-prompt-sanitize-input": knostic_prompt_sanitize_input,
    "/knostic-prompt-sanitize-output": knostic_prompt_sanitize_output,
}


@app.get("/")
@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/debug/loaded-config", dependencies=[Depends(require_bearer)])
async def debug_loaded_config() -> dict:
    """Return non-secret runtime configuration for post-deploy verification."""
    input_routes = [p for p in RAIL_ROUTES if p.endswith("-input")]
    output_routes = [p for p in RAIL_ROUTES if p.endswith("-output")]
    return {
        "wrapper_version": os.environ.get("BUILD_REF", "unknown"),
        "knostic_api_base_env": os.environ.get("KNOSTIC_API_BASE", DEFAULT_API_BASE),
        "knostic_inspect_path": os.environ.get("KNOSTIC_INSPECT_PATH", DEFAULT_INSPECT_PATH),
        "knostic_sanitize_path": os.environ.get("KNOSTIC_SANITIZE_PATH", DEFAULT_SANITIZE_PATH),
        "default_timeout": DEFAULT_TIMEOUT,
        "knostic_api_key_configured": bool(os.environ.get("KNOSTIC_API_KEY", "").strip()),
        "knostic_policy_id_configured": bool(os.environ.get("KNOSTIC_POLICY_ID", "").strip()),
        "wrapper_auth_enabled": bool(WRAPPER_API_KEY),
        "routes": {"input": input_routes, "output": output_routes},
    }


for path, fn in RAIL_ROUTES.items():
    app.add_api_route(
        path,
        endpoint=fn,
        methods=["POST"],
        status_code=200,
        dependencies=[Depends(require_bearer)],
    )


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
