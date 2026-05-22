"""Deploy the Guardrails AI wrapper to TrueFoundry as a Service.

Usage:
    pip install -U "truefoundry"
    tfy login
    # Fill in .env (or edit defaults below), then:
    python deploy.py --wait

What this does:
    1. Builds the image from the existing Dockerfile, passing GUARDRAILS_TOKEN
       as a build arg so hub validators install at build time.
    2. Exposes port 8000 on a TrueFoundry-managed HTTPS endpoint.
    3. Wires WRAPPER_API_KEY + GUARDRAILS_TOKEN as runtime env vars via secret
       references so neither value lives in the image history.
    4. Configures health probes against /health.

Unlike NeMo, this wrapper does NOT call back to the TFY gateway at runtime --
all validators are local. So TFY_BASE_URL and JUDGE_MODEL are absent.
"""

import argparse
import os
import subprocess
import sys
import time

from dotenv import load_dotenv
from truefoundry.deploy import (
    Build,
    DockerFileBuild,
    HealthProbe,
    HttpProbe,
    Port,
    Resources,
    Service,
    get_application,
)


def _build_ref() -> str:
    """Short identifier for this deploy. Surfaces as `wrapper_version` on /debug/loaded-config."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        dirty = subprocess.call(
            ["git", "diff", "--quiet", "--exit-code"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return f"{sha}{'-dirty' if dirty else ''}"
    except Exception:
        return f"deploy-{int(time.time())}"

# override=True makes .env the source of truth.
load_dotenv(override=True)

# The TFY SDK reserves TFY_API_KEY for its own auth. Our .env has runtime
# values for WRAPPER_API_KEY and GUARDRAILS_TOKEN that the SDK shouldn't see.
for _runtime_only in ("TFY_API_KEY", "WRAPPER_API_KEY", "GUARDRAILS_TOKEN"):
    os.environ.pop(_runtime_only, None)

WORKSPACE_FQN = os.environ.get("TFY_WORKSPACE_FQN", "<cluster>:<workspace>")
PUBLIC_HOST = os.environ.get("TFY_PUBLIC_HOST", "ml.<cluster>.truefoundry.cloud")

_raw_path = os.environ.get("TFY_PUBLIC_PATH", "").strip()
if _raw_path:
    if not _raw_path.startswith("/"):
        _raw_path = "/" + _raw_path
    if not _raw_path.endswith("/"):
        _raw_path = _raw_path + "/"
PUBLIC_PATH = _raw_path or None

WRAPPER_API_KEY_SECRET_FQN = os.environ.get(
    "WRAPPER_API_KEY_SECRET_FQN",
    "tfy-secret://<workspace>/guardrails-ai-tfy/wrapper-api-key",
)
GUARDRAILS_TOKEN_SECRET_FQN = os.environ.get(
    "GUARDRAILS_TOKEN_SECRET_FQN",
    "tfy-secret://<workspace>/guardrails-ai-tfy/guardrails-token",
)

BUILD_REF = _build_ref()


def build_service() -> Service:
    return Service(
        name="guardrails-ai-tfy",
        image=Build(
            build_spec=DockerFileBuild(
                dockerfile_path="./Dockerfile",
                build_args={"GUARDRAILS_TOKEN": GUARDRAILS_TOKEN_SECRET_FQN},
            )
        ),
        ports=[
            Port(
                port=8000,
                host=PUBLIC_HOST,
                **({"path": PUBLIC_PATH} if PUBLIC_PATH else {}),
                protocol="TCP",
                expose=True,
            )
        ],
        env={
            "WRAPPER_API_KEY": WRAPPER_API_KEY_SECRET_FQN,
            # GUARDRAILS_TOKEN is intentionally NOT a runtime env var. It's
            # consumed at build time by setup.py; validators live in the image.
            "PORT": "8000",
            "LOG_LEVEL": "info",
            "BUILD_REF": BUILD_REF,
        },
        resources=Resources(
            cpu_request=0.25,
            cpu_limit=1.0,
            memory_request=1024,
            memory_limit=3072,
            ephemeral_storage_request=1024,
            ephemeral_storage_limit=4096,
        ),
        liveness_probe=HealthProbe(
            config=HttpProbe(path="/health", port=8000),
            initial_delay_seconds=30,
            period_seconds=30,
            failure_threshold=3,
        ),
        readiness_probe=HealthProbe(
            config=HttpProbe(path="/health", port=8000),
            initial_delay_seconds=15,
            period_seconds=10,
            failure_threshold=5,
        ),
        replicas=1,
    )


def _check_placeholders() -> None:
    fields = {
        "WORKSPACE_FQN": WORKSPACE_FQN,
        "PUBLIC_HOST": PUBLIC_HOST,
        "WRAPPER_API_KEY_SECRET_FQN": WRAPPER_API_KEY_SECRET_FQN,
        "GUARDRAILS_TOKEN_SECRET_FQN": GUARDRAILS_TOKEN_SECRET_FQN,
    }
    unfilled = [name for name, val in fields.items() if "<" in val or ">" in val]
    if unfilled:
        raise SystemExit(
            "Placeholder values still present for: "
            + ", ".join(unfilled)
            + "\nFix via .env or by editing the defaults at the top of deploy.py."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()

    _check_placeholders()
    service = build_service()
    service.deploy(workspace_fqn=WORKSPACE_FQN, wait=args.wait)

    # service.deploy(wait=True) has been observed to return success while the
    # remote build was still in flight and later transitioned to BUILD_FAILED
    # (image push hung past TFY's build timeout). Verify the version we just
    # submitted actually became active.
    if args.wait:
        app_fqn = f"{WORKSPACE_FQN}:guardrails-ai-tfy"
        app = get_application(app_fqn)
        if app.activeVersion != app.lastVersion:
            print(
                f"\nDEPLOY FAILED: lastVersion={app.lastVersion} but activeVersion={app.activeVersion}.",
                file=sys.stderr,
            )
            print(
                f"The build for v{app.lastVersion} did not reach DEPLOY_SUCCESS; the live pod is still v{app.activeVersion}.",
                file=sys.stderr,
            )
            print(
                f"Check build logs in the TFY dashboard under application id {app.id}.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"\nVerified: activeVersion == lastVersion == {app.activeVersion}, build_ref={BUILD_REF}")

    base = f"https://{PUBLIC_HOST}{(PUBLIC_PATH or '').rstrip('/')}"
    print(f"\nDeployed. Endpoints:")
    print(f"  health             : {base}/health")
    print(f"  debug              : {base}/debug/loaded-config")
    print(f"  detect-pii-input   : {base}/detect-pii-input")
    print(f"  detect-pii-output  : {base}/detect-pii-output")
    print(f"  secrets-present-*  : {base}/secrets-present-input ; {base}/secrets-present-output")
    print(f"  toxic-language-*   : {base}/toxic-language-input ; {base}/toxic-language-output")
    print(f"  profanity-free-out : {base}/profanity-free-output")
    print(
        "\nNext: register each rail as its own Custom Guardrail Config in the TFY dashboard.\n"
        "Operation = Validate; Auth = Custom Bearer Auth with wrapper-api-key.\n"
        "With gateway commit a1c551be+, Fail on error: false is the correct default."
    )
