"""HTTP proxy to Verra's hosted guardrail endpoints. Env read at call time."""

from __future__ import annotations

import os
from typing import Any

import httpx


def call_verra(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    verra_key = os.environ.get("VERRA_KEY", "").strip()
    if not verra_key:
        raise RuntimeError("VERRA_KEY env var is required")
    base = os.environ.get("VERRA_API_BASE", "https://api.helloverra.com").rstrip("/")
    timeout = float(os.environ.get("VERRA_TIMEOUT_SECONDS", "30"))

    with httpx.Client(base_url=base, timeout=timeout) as client:
        response = client.post(
            f"/v1/truefoundry/{path}",
            json=payload,
            headers={"Authorization": f"Bearer {verra_key}"},
        )
        response.raise_for_status()
        return response.json()
