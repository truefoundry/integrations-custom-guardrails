"""FastAPI app for the HiddenLayer custom-guardrail wrapper.

HiddenLayer AIDR Detection v2 powers validate and mutate rails.
This service translates TrueFoundry guardrail requests to HiddenLayer v2 APIs:

    Validate rails  -> POST /detection/v2/interaction-evaluations
    Mutate rails    -> POST /detection/v2/request-evaluations (input)
                       POST /detection/v2/response-evaluations (output)

Endpoints:
    GET  /                       health check (open)
    GET  /health                 health check (open)
    POST /validate-input         validate input (llm_input hook, Operation: Validate)
    POST /validate-output        validate output (llm_output hook, Operation: Validate)
    POST /redact-input           redact input (llm_input hook, Operation: Mutate)
    POST /redact-output          redact output (llm_output hook, Operation: Mutate)
    GET  /debug/loaded-config    bearer-auth gated diagnostics

Response contract:
    Allow  -> HTTP 200 + {"verdict": true}
    Block  -> HTTP 200 + {"verdict": false, "message": "..."}
    Redact -> HTTP 200 + {"verdict": true, "transformed": true, "result": {...}}
    Error  -> HTTP 4xx/5xx (infra / misconfiguration only)
"""

from __future__ import annotations

import hmac
import logging
import os

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from guardrail._hiddenlayer_client import (
    DEFAULT_REGION,
    DEFAULT_TIMEOUT,
    REGION_ENDPOINTS,
)
from guardrail.redact import redact_input, redact_output
from guardrail.validate import validate_input, validate_output

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("hiddenlayer-guardrails-tfy")

# Known-insecure placeholder(s) shipped in .env.example — must never be accepted as a real key.
_PLACEHOLDER_API_KEYS = frozenset({"change-me-to-a-random-string"})


def _wrapper_api_key() -> str:
    return os.environ.get("WRAPPER_API_KEY", "").strip()


def _allow_no_auth() -> bool:
    return os.environ.get("ALLOW_NO_AUTH", "").strip().lower() in ("1", "true", "yes")


def _auth_is_configured() -> bool:
    """True only when a real (non-empty, non-placeholder) wrapper API key is set."""
    key = _wrapper_api_key()
    return bool(key) and key not in _PLACEHOLDER_API_KEYS


def require_bearer(request: Request) -> None:
    """Bearer-auth dependency. Fails CLOSED when no real key is configured.

    A missing/empty/placeholder WRAPPER_API_KEY no longer disables auth silently: every
    guarded route returns HTTP 503 unless ALLOW_NO_AUTH=true is set explicitly (local dev
    only). This prevents a deploy that forgot/failed to inject the secret from silently
    serving an unauthenticated guardrail.
    """
    if not _auth_is_configured():
        if _allow_no_auth():
            return
        raise HTTPException(
            status_code=503,
            detail="Guardrail service misconfigured: wrapper authentication is not configured.",
        )
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    provided = header.split(" ", 1)[1].strip()
    if not hmac.compare_digest(provided, _wrapper_api_key()):
        raise HTTPException(status_code=401, detail="invalid bearer token")


if not _auth_is_configured():
    if _allow_no_auth():
        log.warning(
            "WRAPPER_API_KEY is unset/placeholder and ALLOW_NO_AUTH is enabled: bearer auth is "
            "DISABLED. Local development only — never deploy without a real WRAPPER_API_KEY."
        )
    else:
        log.error(
            "WRAPPER_API_KEY is missing, empty, or the example placeholder: bearer auth cannot be "
            "enforced. Failing CLOSED (HTTP 503) on all guardrail routes. Set a strong "
            "WRAPPER_API_KEY, or set ALLOW_NO_AUTH=true for local development only."
        )


app = FastAPI(
    title="hiddenlayer-guardrails-tfy",
    description="HiddenLayer AIDR Detection v2 rails behind the TrueFoundry custom-guardrail contract.",
    version="2.0.0",
)


@app.get("/")
@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


RAIL_ROUTES: dict[str, object] = {
    "/validate-input": validate_input,
    "/validate-output": validate_output,
    "/redact-input": redact_input,
    "/redact-output": redact_output,
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
    region = os.environ.get("HIDDENLAYER_REGION", DEFAULT_REGION).strip().lower()
    endpoints = REGION_ENDPOINTS.get(region, REGION_ENDPOINTS[DEFAULT_REGION])
    client_id_configured = bool(os.environ.get("HIDDENLAYER_CLIENT_ID", "").strip())
    client_secret_configured = bool(os.environ.get("HIDDENLAYER_CLIENT_SECRET", "").strip())
    return {
        "routes": {
            "input": [p for p in RAIL_ROUTES if p.endswith("-input")],
            "output": [p for p in RAIL_ROUTES if p.endswith("-output")],
        },
        "hiddenlayer_region": region,
        "hiddenlayer_api_version": "v2",
        "hiddenlayer_request_evaluations_path": "/detection/v2/request-evaluations",
        "hiddenlayer_response_evaluations_path": "/detection/v2/response-evaluations",
        "hiddenlayer_interaction_evaluations_path": "/detection/v2/interaction-evaluations",
        "hiddenlayer_api_base": os.environ.get("HIDDENLAYER_API_BASE", endpoints["api_base"]),
        "hiddenlayer_auth_base": os.environ.get("HIDDENLAYER_AUTH_BASE", endpoints["auth_base"]),
        "hiddenlayer_project_id_configured": bool(os.environ.get("HIDDENLAYER_PROJECT_ID", "").strip()),
        "hiddenlayer_credentials_configured": client_id_configured and client_secret_configured,
        "default_timeout": DEFAULT_TIMEOUT,
        "wrapper_auth_enabled": _auth_is_configured(),
        "wrapper_version": os.environ.get("BUILD_REF", "unknown"),
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
