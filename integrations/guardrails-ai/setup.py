"""Install Guardrails AI Hub validators at Docker build time.

Invoked from the Dockerfile as `python setup.py`. The token is taken from the
GUARDRAILS_TOKEN environment variable, which the Dockerfile receives as a
build arg (TFY_secret reference -> Docker build arg -> env var inside the
build layer; not present at runtime).
"""

import os
import subprocess


GUARDRAILS_TOKEN = os.getenv("GUARDRAILS_TOKEN")
if not GUARDRAILS_TOKEN:
    raise RuntimeError("GUARDRAILS_TOKEN not set. Pass via Docker --build-arg or env var.")


VALIDATORS = [
    "hub://guardrails/detect_pii",
    "hub://guardrails/secrets_present",
    "hub://guardrails/toxic_language",
    "hub://guardrails/profanity_free",
]


def setup_guardrails() -> None:
    subprocess.run(
        [
            "guardrails", "configure",
            "--disable-metrics",
            "--disable-remote-inferencing",
            "--token", GUARDRAILS_TOKEN,
        ],
        check=True,
    )
    for validator in VALIDATORS:
        subprocess.run(["guardrails", "hub", "install", validator, "--quiet"], check=True)


if __name__ == "__main__":
    setup_guardrails()
