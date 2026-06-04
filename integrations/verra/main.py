"""FastAPI app for the Verra custom-guardrail wrapper.

Forwards every TF guardrail request to api.helloverra.com/v1/truefoundry/*
authenticated with VERRA_KEY. Detection runs in Verra's backend; this service
only translates the TF contract.

Endpoints:
    GET  /                          health check (open)
    GET  /health                    health check (open)
    POST /scan-input                validate input
    POST /redact-input              mutate input
    POST /scan-output               validate output
    POST /redact-output             mutate output
    GET  /debug/loaded-config       bearer-auth gated; lists loaded routes

A shared bearer token (WRAPPER_API_KEY) gates rail endpoints and /debug.
Configure the same token in the TrueFoundry dashboard under Custom Bearer Auth.

Response contract:
    Allow  -> HTTP 200 + {"verdict": true}
    Block  -> HTTP 200 + {"verdict": false, "message": "..."}
    Mutate -> HTTP 200 + {"verdict": true, "transformed": <bool>, "result": <body>}
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request

load_dotenv()

from guardrail.redact_input import redact_input  # noqa: E402
from guardrail.redact_output import redact_output  # noqa: E402
from guardrail.scan_input import scan_input  # noqa: E402
from guardrail.scan_output import scan_output  # noqa: E402

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("verra-guardrails-tfy")

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
    title="verra-guardrails-tfy",
    description="Thin proxy from the TrueFoundry custom-guardrail contract to Verra's managed AI governance API.",
    version="1.0.0",
)


@app.get("/")
@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


RAIL_ROUTES: dict[str, object] = {
    "/scan-input": scan_input,
    "/redact-input": redact_input,
    "/scan-output": scan_output,
    "/redact-output": redact_output,
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
    """Diagnostic: list the loaded routes and the build ref."""
    return {
        "routes": {
            "input": [p for p in RAIL_ROUTES if p.endswith("-input")],
            "output": [p for p in RAIL_ROUTES if p.endswith("-output")],
        },
        "verra_api_base": os.environ.get("VERRA_API_BASE", "https://api.helloverra.com"),
        "verra_key_set": bool(os.environ.get("VERRA_KEY", "").strip()),
        "wrapper_version": os.environ.get("BUILD_REF", "unknown"),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
