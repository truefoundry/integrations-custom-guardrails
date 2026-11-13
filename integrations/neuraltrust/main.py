"""NeuralTrust TrustGuard wrapper for the TrueFoundry AI Gateway (custom guardrail).

Endpoints:
    GET  /, /health                health check (open)
    GET  /debug/loaded-config      bearer-auth gated; diagnostic
    POST /validate-input           LLM input   - Operation: Validate
    POST /validate-output          LLM output  - Operation: Validate
    POST /redact-input             LLM input   - Operation: Mutate (applies TrustGuard DLP transform)
    POST /redact-output            LLM output  - Operation: Mutate

MCP Tool Pre/Post-Invoke hooks are out of scope for v1 (the custom-guardrail payload for
MCP hooks is not publicly documented by TrueFoundry).

Response contract: HTTP 200 + verdict JSON for every policy decision; 5xx only when
TrustGuard could not produce a verdict. See guardrail/rails.py for the mapping.
"""

from __future__ import annotations

import hmac
import logging
import os

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request

from guardrail import _trustguard_client as tg
from guardrail.rails import (
    redact_input,
    redact_output,
    validate_input,
    validate_output,
)

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("neuraltrust-guardrails-tfy")


def require_bearer(request: Request) -> None:
    """Bearer-auth dependency. No key configured -> auth disabled (local dev only)."""
    expected = os.environ.get("WRAPPER_API_KEY", "").strip()
    if not expected:
        return
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if not hmac.compare_digest(header.split(" ", 1)[1].strip(), expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")


app = FastAPI(title="neuraltrust-guardrails-tfy", version="0.1.0")


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

for path, fn in RAIL_ROUTES.items():
    app.add_api_route(path, endpoint=fn, methods=["POST"], dependencies=[Depends(require_bearer)])


@app.get("/debug/loaded-config", dependencies=[Depends(require_bearer)])
async def debug_loaded_config() -> dict:
    return {
        "routes": {
            "input": [p for p in RAIL_ROUTES if p.endswith("-input")],
            "output": [p for p in RAIL_ROUTES if p.endswith("-output")],
        },
        "neuraltrust_base_url": tg._settings()[0],
        "timeout_seconds": tg.resolve_timeout(),
        "neuraltrust_api_key_configured": bool(os.environ.get("NEURALTRUST_API_KEY", "").strip()),
        "ask_action": os.environ.get("TRUSTGUARD_ASK_ACTION", "block"),
        "transform_on_validate": os.environ.get("TRUSTGUARD_TRANSFORM_ON_VALIDATE", "allow"),
        "wrapper_version": os.environ.get("BUILD_REF", "unknown"),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
