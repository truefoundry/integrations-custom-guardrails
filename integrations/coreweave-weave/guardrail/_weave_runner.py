"""Singleton holding the CoreWeave Weave toxicity scorer.

Loaded at module import time so both rail handlers (`toxicity_input`,
`toxicity_output`) share one in-memory copy of the Celadon model
(~550 MB on disk, ~600 MB resident). If the model fails to load, the
import raises and the FastAPI process refuses to start -- loud and correct.

The scorer itself is stateless across calls. Threshold configuration is
applied per-request (not on the singleton) so dashboard `config` JSON can
override defaults without restart.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from weave.scorers import WeaveToxicityScorerV1

log = logging.getLogger(__name__)

# Override via the per-request `config` field on the guardrail request
# (see toxicity_input.py). Tuned away from Weave's own defaults:
# - category_threshold raised from 2 to 3 because Celadon scores benign
#   short capitalized greetings like "Hi" and "Hey" at Race/Origin=2,
#   which produced playground false positives at threshold=2. Score 3+
#   reliably indicates real hate / death threats / overt slurs.
DEFAULT_TOTAL_THRESHOLD = 5
DEFAULT_CATEGORY_THRESHOLD = 3
DEFAULT_AGGREGATION = "max"


def _load_scorer() -> WeaveToxicityScorerV1:
    device = os.environ.get("WEAVE_TOXICITY_DEVICE", "cpu")
    log.info("loading WeaveToxicityScorerV1 (device=%s)", device)
    s = WeaveToxicityScorerV1(device=device)
    # Warmup: first inference call after model load is ~10x slower (~250 ms vs
    # ~25 ms steady-state). Run one throwaway call so the readiness probe sees
    # a fully-warm pod and the first real request lands on the fast path.
    s.score(output="warmup")
    log.info("WeaveToxicityScorerV1 ready")
    return s


scorer = _load_scorer()


def evaluate(text: str, config: dict[str, Any] | None) -> dict[str, Any]:
    """Run the scorer and return a verdict-friendly payload.

    Reads thresholds from per-request config with fallback to defaults so the
    dashboard's Config JSON can tune sensitivity without redeploying.
    """
    config = config or {}
    total_threshold = int(config.get("total_threshold", DEFAULT_TOTAL_THRESHOLD))
    category_threshold = int(config.get("category_threshold", DEFAULT_CATEGORY_THRESHOLD))

    result = scorer.score(output=text).model_dump()
    metadata: dict[str, int] = result.get("metadata") or {}

    top_category, top_score = ("", 0)
    if metadata:
        top_category, top_score = max(metadata.items(), key=lambda kv: kv[1])
    total = sum(metadata.values())

    # Weave's own `passed` field uses the scorer's instance thresholds. We
    # recompute against the per-request thresholds so dashboard config tuning
    # takes effect without rebuilding the singleton.
    passed = not (total > total_threshold or top_score >= category_threshold)

    return {
        "passed": passed,
        "metadata": metadata,
        "total": total,
        "top_category": top_category,
        "top_score": top_score,
        "thresholds": {
            "total": total_threshold,
            "category": category_threshold,
        },
    }
