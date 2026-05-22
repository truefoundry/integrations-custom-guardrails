"""Shared NeMo Guardrails runner instantiated once at import time.

Both `guardrail/self_check_input.py` and `guardrail/self_check_output.py` import
the same `runner` instance from this module. Initialization is expensive
(NeMo config load + LLM client construction) so we share state across handlers.

We run NeMo in rails-only mode: `passthrough=True` in config.yml plus
per-request `GenerationRailsOptions` that disable dialog/output/retrieval when
running only the input rail (and vice versa). One LLM judge call per rail run.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nemoguardrails import LLMRails, RailsConfig
from nemoguardrails.rails.llm.options import GenerationOptions

log = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get("NEMO_CONFIG_PATH", "config")).resolve()

# Decision tokens NeMo emits when a rail aborts the flow.
BLOCK_DECISIONS = {"stop", "refuse", "abort", "block"}


def _materialize_config(src: Path) -> Path:
    """Copy src into a fresh tempdir, expanding ${ENV_VAR} in .yml/.yaml files.

    NeMo Guardrails does not interpolate env vars in its YAML, so we do it here
    before handing the directory to RailsConfig.from_path.
    """
    dst = Path(tempfile.mkdtemp(prefix="nemo-rails-"))
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if item.suffix.lower() in {".yml", ".yaml"}:
            target.write_text(os.path.expandvars(item.read_text()))
        else:
            shutil.copy2(item, target)
    return dst


@dataclass
class RailVerdict:
    """Internal verdict shape produced by RailsRunner; translated to ValidateGuardrailResponse in handlers."""

    decision: str  # "allow" | "block"
    refusal: str | None = None
    activated: list[str] = field(default_factory=list)


class RailsRunner:
    def __init__(self, config_dir: Path = CONFIG_DIR) -> None:
        log.info("Loading NeMo Guardrails config from %s", config_dir)
        expanded = _materialize_config(config_dir)
        log.info("Expanded config materialized at %s", expanded)
        self._expanded_dir = expanded
        self._config = RailsConfig.from_path(str(expanded))
        self._rails = LLMRails(self._config)

    async def check_input(self, user_message: str) -> RailVerdict:
        options = GenerationOptions(
            rails={"input": True, "dialog": False, "output": False, "retrieval": False},
            log={"activated_rails": True},
        )
        result = await self._rails.generate_async(
            messages=[{"role": "user", "content": user_message}],
            options=options,
        )
        return self._verdict(result, original=user_message)

    async def check_output(self, last_user_message: str, assistant_message: str) -> RailVerdict:
        options = GenerationOptions(
            rails={"input": False, "dialog": False, "output": True, "retrieval": False},
            log={"activated_rails": True},
        )
        messages = [
            {"role": "user", "content": last_user_message or ""},
            {"role": "assistant", "content": assistant_message},
        ]
        result = await self._rails.generate_async(
            messages=messages,
            options=options,
        )
        return self._verdict(result, original=assistant_message)

    @staticmethod
    def _verdict(result: Any, *, original: str) -> RailVerdict:
        response = getattr(result, "response", result)
        if isinstance(response, list):
            last = response[-1] if response else {}
        else:
            last = response or {}
        content = (last.get("content") if isinstance(last, dict) else None) or ""

        activated: list[str] = []
        blocked_by_decision = False
        log_obj = getattr(result, "log", None)
        rail_logs = getattr(log_obj, "activated_rails", None) or []
        for r in rail_logs:
            name = getattr(r, "name", None) or (r.get("name") if isinstance(r, dict) else None)
            if name:
                activated.append(name)
            decisions = (
                getattr(r, "decisions", None)
                or (r.get("decisions") if isinstance(r, dict) else None)
                or []
            )
            if any((d or "").lower() in BLOCK_DECISIONS for d in decisions):
                blocked_by_decision = True

        content_changed = content.strip() != (original or "").strip() and bool(content)

        if blocked_by_decision or (activated and content_changed):
            log.info("rail verdict=block  activated=%s  refusal=%r", activated, content[:120])
            return RailVerdict(
                decision="block",
                refusal=content or "Request blocked by NeMo Guardrails.",
                activated=activated,
            )

        log.info("rail verdict=allow  activated=%s", activated)
        return RailVerdict(decision="allow", activated=activated)


# Module-level singleton. Instantiated at import time so startup failures are
# loud (pod doesn't start) rather than per-request errors.
runner = RailsRunner()
