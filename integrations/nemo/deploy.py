"""Deploy the NeMo Guardrails wrapper to TrueFoundry as a Service.

Usage:
    pip install -U "truefoundry"
    tfy login
    # Fill in .env (or edit defaults below), then:
    python deploy.py --wait

What this does:
    1. Builds the image from the existing Dockerfile.
    2. Exposes port 8000 on a TrueFoundry-managed HTTPS endpoint.
    3. Wires env vars + secret references for the LLM judge backend
       and the wrapper's bearer token.
    4. Configures health probes against /health.
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

# override=True makes .env the source of truth at deploy time. Without it,
# a stale value from a prior `source .env` in this shell would silently win.
load_dotenv(override=True)

# The TrueFoundry SDK reserves TFY_API_KEY (paired with TFY_HOST) for its OWN
# auth. Our .env uses TFY_API_KEY for the runtime wrapper's auth to the
# gateway -- a different concept. Strip those local-dev runtime values out
# so the SDK falls back to the `tfy login` session.
for _runtime_only in ("TFY_API_KEY", "WRAPPER_API_KEY"):
    os.environ.pop(_runtime_only, None)

# ---------------------------------------------------------------------------
# Placeholders --- replace via .env or edit defaults before deploying.
# ---------------------------------------------------------------------------

WORKSPACE_FQN = os.environ.get("TFY_WORKSPACE_FQN", "<cluster>:<workspace>")
PUBLIC_HOST = os.environ.get("TFY_PUBLIC_HOST", "ml.<cluster>.truefoundry.cloud")
_raw_path = os.environ.get("TFY_PUBLIC_PATH", "").strip()
# Port.path regex requires the path to begin AND end with `/`. Normalize.
if _raw_path:
    if not _raw_path.startswith("/"):
        _raw_path = "/" + _raw_path
    if not _raw_path.endswith("/"):
        _raw_path = _raw_path + "/"
PUBLIC_PATH = _raw_path or None

TFY_API_KEY_SECRET_FQN = os.environ.get(
    "TFY_API_KEY_SECRET_FQN", "tfy-secret://<workspace>/nemo-guardrails-tfy/tfy-api-key"
)
WRAPPER_API_KEY_SECRET_FQN = os.environ.get(
    "WRAPPER_API_KEY_SECRET_FQN",
    "tfy-secret://<workspace>/nemo-guardrails-tfy/wrapper-api-key",
)

TFY_BASE_URL = os.environ.get(
    "TFY_BASE_URL",
    "https://<tenant>.truefoundry.cloud/api/llm/api/inference/openai/v1",
)
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "openai-main/gpt-4o")

BUILD_REF = _build_ref()


def build_service() -> Service:
    return Service(
        name="nemo-guardrails-tfy",
        image=Build(build_spec=DockerFileBuild(dockerfile_path="./Dockerfile")),
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
            "TFY_BASE_URL": TFY_BASE_URL,
            "TFY_API_KEY": TFY_API_KEY_SECRET_FQN,
            "JUDGE_MODEL": JUDGE_MODEL,
            "WRAPPER_API_KEY": WRAPPER_API_KEY_SECRET_FQN,
            "PORT": "8000",
            "LOG_LEVEL": "info",
            "BUILD_REF": BUILD_REF,
        },
        resources=Resources(
            cpu_request=0.25,
            cpu_limit=1.0,
            memory_request=512,
            memory_limit=2048,
            ephemeral_storage_request=512,
            ephemeral_storage_limit=2048,
        ),
        liveness_probe=HealthProbe(
            config=HttpProbe(path="/health", port=8000),
            initial_delay_seconds=20,
            period_seconds=30,
            failure_threshold=3,
        ),
        readiness_probe=HealthProbe(
            config=HttpProbe(path="/health", port=8000),
            initial_delay_seconds=10,
            period_seconds=10,
            failure_threshold=3,
        ),
        replicas=1,
    )


def _check_placeholders() -> None:
    fields = {
        "WORKSPACE_FQN": WORKSPACE_FQN,
        "PUBLIC_HOST": PUBLIC_HOST,
        "TFY_BASE_URL": TFY_BASE_URL,
        "TFY_API_KEY_SECRET_FQN": TFY_API_KEY_SECRET_FQN,
        "WRAPPER_API_KEY_SECRET_FQN": WRAPPER_API_KEY_SECRET_FQN,
    }
    unfilled = [name for name, val in fields.items() if "<" in val or ">" in val]
    if unfilled:
        raise SystemExit(
            "\n".join(
                [
                    "Placeholder values still present in deploy.py for: " + ", ".join(unfilled),
                    "",
                    "Fix via .env or by editing the defaults at the top of deploy.py.",
                    "",
                    "Find cluster-configured host(s) at: Integrations -> Clusters -> <cluster>.",
                    "Find workspace FQN at: Workspaces -> <workspace> (or `tfy workspace list`).",
                ]
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait", action="store_true", help="block until the deploy is healthy")
    args = parser.parse_args()

    _check_placeholders()
    service = build_service()
    service.deploy(workspace_fqn=WORKSPACE_FQN, wait=args.wait)

    # service.deploy(wait=True) has been observed to return success while the
    # remote build was still in flight and later transitioned to BUILD_FAILED.
    # Verify the version we just submitted actually became active.
    if args.wait:
        app_fqn = f"{WORKSPACE_FQN}:nemo-guardrails-tfy"
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
    print(f"  health         : {base}/health")
    print(f"  input rail URL : {base}/self-check-input")
    print(f"  output rail URL: {base}/self-check-output")
    print(f"  debug          : {base}/debug/loaded-config")
    print(
        "\nNext: register each rail as its own Custom Guardrail Config in the TFY dashboard.\n"
        "Operation = Validate; Auth = Custom Bearer Auth with wrapper-api-key.\n"
        "With gateway commit a1c551be+, Fail on error: false is the correct default."
    )
