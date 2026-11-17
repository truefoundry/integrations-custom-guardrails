"""FastAPI app for the Bigeye access-decision custom guardrail.

Endpoints:
    GET  /                            health check (open)
    GET  /health                      health check (open)
    GET  /debug/loaded-config         non-secret runtime summary (open)
    POST /mcp-pre-invoke              MCP tool pre-invoke rail (Operation: Validate)

Response contract: HTTP 200 with a verdict for both allow and block. Non-2xx is
reserved for real errors: 401 missing key, 422 malformed payload, 500
misconfiguration, 502 Bigeye error, 504 Bigeye timeout. Never 429 or 503,
which the gateway retries. See docs/gateway-contract.md.

Auth: the guardrail's Bearer token is the Bigeye API key, forwarded to Bigeye
as ``Authorization: <auth_scheme> <key>``. There is no separate wrapper key.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from guardrail._bigeye_client import BigeyeCallError, close_client, get_client
from guardrail._decision import DecisionError
from guardrail._payload_log import payload_logging_enabled
from guardrail._settings import (
    DEFAULT_AGENT_ID,
    DEFAULT_BIGEYE_URL,
    DEFAULT_BLOCK_ON,
    DEFAULT_OPERATION,
    DEFAULT_TIMEOUT_MS,
    ConfigError,
    allowed_hosts,
)
from guardrail.mcp_pre_invoke import mcp_pre_invoke

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bigeye-guardrails-tfy")


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_client()
    yield
    await close_client()


app = FastAPI(
    title="bigeye-guardrails-tfy",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's default 422 echoes the offending input; the gateway logs error bodies, so drop it."""
    problems = [{"loc": list(err.get("loc", ())), "msg": err.get("msg", "")} for err in exc.errors()]
    return JSONResponse(status_code=422, content={"error": "invalid_payload", "detail": problems})


@app.exception_handler(ConfigError)
async def config_error_handler(_: Request, exc: ConfigError) -> JSONResponse:
    return JSONResponse(status_code=500, content={"error": "configuration_error", "detail": str(exc)})


@app.exception_handler(BigeyeCallError)
async def bigeye_call_error_handler(_: Request, exc: BigeyeCallError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": "bigeye_error", "detail": exc.detail})


@app.exception_handler(DecisionError)
async def decision_error_handler(_: Request, exc: DecisionError) -> JSONResponse:
    log.error("%s", exc)
    return JSONResponse(status_code=502, content={"error": "bigeye_error", "detail": str(exc)})


@app.get("/")
@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


RAIL_ROUTES: dict[str, object] = {
    "/mcp-pre-invoke": mcp_pre_invoke,
}

for path, fn in RAIL_ROUTES.items():
    app.add_api_route(path, endpoint=fn, methods=["POST"])


@app.get("/debug/loaded-config")
async def debug_loaded_config() -> dict:
    """What the running pod has loaded. Non-secret values only."""
    return {
        "routes": {"mcp_pre_invoke": list(RAIL_ROUTES)},
        "wrapper_version": os.environ.get("BUILD_REF", "unknown"),
        "allowed_hosts": sorted(allowed_hosts()),
        "log_payloads": payload_logging_enabled(),
        "defaults": {
            "bigeye_url": DEFAULT_BIGEYE_URL,
            "auth_scheme": "apikey",
            "block_on": list(DEFAULT_BLOCK_ON),
            "evaluate_policy": False,
            "bigeye_timeout_ms": DEFAULT_TIMEOUT_MS,
            "agent_id": DEFAULT_AGENT_ID,
            "operation": DEFAULT_OPERATION,
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
