"""FastAPI app for the Guardrails AI Hub validators wrapper.

Endpoints (one route per validator per direction; matches template convention):

    GET  /                          health check (open)
    GET  /health                    health check (open)
    POST /detect-pii-input          DetectPII on user input
    POST /detect-pii-output         DetectPII on assistant response
    POST /secrets-present-input     SecretsPresent on user input
    POST /secrets-present-output    SecretsPresent on assistant response
    POST /toxic-language-input      ToxicLanguage on user input
    POST /toxic-language-output     ToxicLanguage on assistant response
    POST /profanity-free-output     ProfanityFree on assistant response (output-only)
    GET  /debug/loaded-config       bearer-auth gated; lists loaded validators

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

from guardrail.detect_pii_input import detect_pii_input
from guardrail.detect_pii_output import detect_pii_output
from guardrail.profanity_free_output import profanity_free_output
from guardrail.secrets_present_input import secrets_present_input
from guardrail.secrets_present_output import secrets_present_output
from guardrail.toxic_language_input import toxic_language_input
from guardrail.toxic_language_output import toxic_language_output

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("guardrails-ai-tfy")

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
    title="guardrails-ai-tfy",
    description="Guardrails AI Hub validators behind the TrueFoundry custom-guardrail contract.",
    version="2.0.0",
)


@app.get("/")
@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


# Mapping of route → function for cleaner registration and the debug endpoint.
RAIL_ROUTES: dict[str, object] = {
    "/detect-pii-input": detect_pii_input,
    "/detect-pii-output": detect_pii_output,
    "/secrets-present-input": secrets_present_input,
    "/secrets-present-output": secrets_present_output,
    "/toxic-language-input": toxic_language_input,
    "/toxic-language-output": toxic_language_output,
    "/profanity-free-output": profanity_free_output,
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
    """Diagnostic: list the loaded validators (one per route) and the build ref."""
    input_routes = [p for p in RAIL_ROUTES if p.endswith("-input")]
    output_routes = [p for p in RAIL_ROUTES if p.endswith("-output")]
    return {
        "routes": {
            "input": input_routes,
            "output": output_routes,
        },
        "wrapper_version": os.environ.get("BUILD_REF", "unknown"),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
