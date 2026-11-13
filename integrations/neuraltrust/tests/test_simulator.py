"""Offline check of scripts/simulate_gateway.py gateway semantics (TrustGuard stubbed)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["WRAPPER_API_KEY"] = "test-wrapper-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import main  # noqa: E402
import simulate_gateway as sim  # noqa: E402
from guardrail import _trustguard_client as tg  # noqa: E402


def test_strip_and_restore_system():
    body = sim.req(("system", "s"), ("user", "u"), ("assistant", "a"), ("system", "s2"), ("user", "u2"))
    sent, removed = sim.strip_system(body)
    assert [m["role"] for m in sent["messages"]] == ["user", "assistant", "user"]
    assert sim.restore_system(sent, removed) == body


@pytest.mark.parametrize(
    "status,data,strategy,op,expected",
    [
        (200, {"verdict": True}, "enforce", "validate", "ALLOWED"),
        (200, {"verdict": False, "message": "x"}, "enforce", "validate", "BLOCKED"),
        (200, {"verdict": False}, "enforce_but_ignore_on_error", "validate", "BLOCKED"),
        (200, {"verdict": False}, "audit", "validate", "ALLOWED"),
        (502, {"detail": "x"}, "enforce", "validate", "BLOCKED"),
        (502, {"detail": "x"}, "enforce_but_ignore_on_error", "validate", "ALLOWED"),
        (None, None, "audit", "validate", "ALLOWED"),
    ],
)
def test_interpret(status, data, strategy, op, expected):
    assert sim.interpret(status, data, None, operation=op, strategy=strategy, original={}, ms=1).decision == expected


def test_mutate_result_applied_only_on_mutate():
    data = {"verdict": True, "transformed": True, "result": {"messages": [{"role": "user", "content": "m"}]}}
    assert sim.interpret(200, data, None, operation="mutate", strategy="enforce", original={}, ms=1).reason == "mutated"
    assert sim.interpret(200, data, None, operation="validate", strategy="enforce", original={}, ms=1).reason == "verdict:true"


def test_simulated_mutate_against_wrapper(monkeypatch):
    async def fake(**kw):
        msgs = kw["payload"]["messages"]
        return {"status": "transform",
                "transformed_payload": {"messages": [{**m, "content": "[MASKED_EMAIL]"} for m in msgs]}}

    monkeypatch.setattr(tg, "evaluate", fake)
    with TestClient(main.app) as c:
        body = sim.req(("system", "sys"), ("user", "a@b.com"))
        o = sim.run_input(c.post, "/redact-input", body, operation="mutate", strategy="enforce",
                          key="test-wrapper-key", gateway_timeout=5)
    assert o.decision == "ALLOWED" and o.reason == "mutated"
    assert o.body["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "[MASKED_EMAIL]"}]
