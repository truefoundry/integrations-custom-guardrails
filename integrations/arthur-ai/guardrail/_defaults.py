"""Built-in Arthur checks used when TrueFoundry sends no config.checks.

Defaults mirror the Arthur GenAI Engine stateless validate contract:
https://engine.platform.arthur.ai/docs#/Stateless%20Validation/stateless_validate_api_v2_validate_post

Dashboard config.checks still overrides these when provided.
"""

from __future__ import annotations

from typing import Any

# Input rail: prompt injection + toxicity on user messages.
DEFAULT_INPUT_CHECKS: list[dict[str, Any]] = [
    {
        "name": "prompt-injection-check",
        "type": "PromptInjectionRule",
        "apply_to_prompt": True,
        "apply_to_response": False,
    },
    {
        "name": "toxicity-check",
        "type": "ToxicityRule",
        "apply_to_prompt": True,
        "apply_to_response": False,
        "config": {"threshold": 0.5},
    },
]

# Output rail: toxicity on assistant completions.
DEFAULT_OUTPUT_CHECKS: list[dict[str, Any]] = [
    {
        "name": "toxicity-check",
        "type": "ToxicityRule",
        "apply_to_prompt": False,
        "apply_to_response": True,
        "config": {"threshold": 0.5},
    },
]
