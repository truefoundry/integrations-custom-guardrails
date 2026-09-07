"""RepelloAI Argus custom-guardrail wrapper for the TrueFoundry AI Gateway.

Endpoints:
    GET  /                     health check (open)
    GET  /health               health check (open)
    GET  /debug/loaded-config  bearer-gated diagnostics
    POST /validate-input       input rail, dashboard Operation: Validate
    POST /validate-output      output rail, dashboard Operation: Validate
    POST /redact-input         input rail, dashboard Operation: Mutate
    POST /redact-output        output rail, dashboard Operation: Mutate

Every rail returns HTTP 200 with a JSON verdict. Non-2xx is reserved for real
failures, which the gateway's `Fail on error` toggle governs. Register one
operation per hook: two rails on the same hook each return a full body, and the
second overwrites the first. See docs/gateway-contract.md.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets as secrets_mod
from collections.abc import AsyncIterator

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from guardrail.argus import (
    SAVE_EVENTS,
    ArgusApiError,
    api_base,
    argus_input,
    argus_output,
    close_client,
    init_client,
    max_text_chars,
    resolve_asset_id,
    stats_snapshot,
    timeout_seconds,
    verify_asset,
)
from guardrail.redact import redact_input, redact_output

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("repelloai-argus-guardrails-tfy")

REQUIRED_ENV_VARS = ("ARGUS_API_KEY", "ARGUS_ASSET_ID", "WRAPPER_API_KEY")

# The dashboard Operation each route expects. A mismatch is otherwise silent:
# a mutate-shaped response registered under `Operation: Validate` still returns
# 200 while the gateway discards the redaction.
RAIL_ROUTES = {
    "/validate-input": (argus_input, "Validate"),
    "/validate-output": (argus_output, "Validate"),
    "/redact-input": (redact_input, "Mutate"),
    "/redact-output": (redact_output, "Mutate"),
}

_startup_state: dict[str, object] = {"asset_verified": None}


def _require_env() -> None:
    """Fail startup on missing configuration. A wrapper that boots without
    credentials passes its health check and then 5xxs every call, which under
    `Fail on error: false` lets traffic through unguarded."""
    missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Refusing to start; see .env.example."
        )


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _require_env()
    init_client()
    try:
        asset_id = resolve_asset_id(None)
        valid = await verify_asset(asset_id)
        _startup_state["asset_verified"] = valid
        if not valid:
            raise RuntimeError(
                f"Argus rejected asset_id {asset_id!r}. Refusing to start: an "
                "invalid asset returns 'passed' for every scan, so the guardrail "
                "would allow all traffic."
            )
        log.info("Argus asset %s verified; wrapper ready", asset_id)
        yield
    finally:
        await close_client()


app = FastAPI(
    title="repelloai-argus-guardrails-tfy",
    version="1.0.0",
    lifespan=lifespan,
)


def require_bearer(request: Request) -> None:
    """Bearer-auth dependency using a constant-time comparison."""
    expected = os.environ.get("WRAPPER_API_KEY", "").strip()
    if not expected:
        raise HTTPException(status_code=500, detail="WRAPPER_API_KEY is not configured")
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    presented = header.split(" ", 1)[1].strip()
    if not secrets_mod.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")


@app.exception_handler(ArgusApiError)
async def argus_error_handler(request: Request, exc: ArgusApiError) -> JSONResponse:
    """Return upstream failures as non-2xx. The Argus response body is never
    logged: `details.text` holds detected secrets in plaintext."""
    log.error("Argus rail failure [%s]: %s", exc.error, exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.error, "detail": exc.detail},
    )


@app.get("/")
@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


for path, (fn, _operation) in RAIL_ROUTES.items():
    app.add_api_route(
        path,
        endpoint=fn,
        methods=["POST"],
        dependencies=[Depends(require_bearer)],
    )


@app.get("/debug/loaded-config", dependencies=[Depends(require_bearer)])
async def debug_loaded_config() -> dict:
    """Report runtime configuration and scan counters. Argus has no endpoint
    listing an asset's policies, so `scan_stats` is the only way to tell a
    working asset from one with nothing enabled."""

    def describe_secret(name: str) -> dict[str, object]:
        value = os.environ.get(name, "").strip()
        return {"present": bool(value), "length": len(value)}

    return {
        "wrapper_version": os.environ.get("BUILD_REF", "unknown"),
        "routes": {
            "input": [p for p in RAIL_ROUTES if p.endswith("-input")],
            "output": [p for p in RAIL_ROUTES if p.endswith("-output")],
        },
        "dashboard_operations": {
            path: operation for path, (_fn, operation) in RAIL_ROUTES.items()
        },
        "argus": {
            "api_base": api_base(),
            "asset_id": os.environ.get("ARGUS_ASSET_ID", ""),
            "asset_verified_at_startup": _startup_state["asset_verified"],
            "timeout_seconds": timeout_seconds(),
            "max_text_chars": max_text_chars(),
            "save_events": SAVE_EVENTS,
        },
        "secrets": {name: describe_secret(name) for name in ("ARGUS_API_KEY", "WRAPPER_API_KEY")},
        "scan_stats": stats_snapshot(),
        "note": (
            "observed_policies lists policy names seen in violations since "
            "startup. Empty after many scans suggests the Argus asset has no "
            "active policies, or policies that are enabled but unconfigured."
        ),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
