"""The guardrail's Bearer token is the Bigeye API key; the wrapper forwards it and stores nothing."""

from __future__ import annotations

from fastapi import HTTPException, Request

MISSING_KEY_DETAIL = "missing bearer token: set the guardrail's Bearer token to a Bigeye API key"


def bigeye_api_key(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail=MISSING_KEY_DETAIL)
    return token.strip()
