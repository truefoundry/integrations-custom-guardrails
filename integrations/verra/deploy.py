"""Deploy the Verra wrapper to TrueFoundry as a Service.

Usage:
    pip install -U "truefoundry"
    tfy login
    # Fill in .env (or edit defaults below), then:
    python deploy.py --wait

The wrapper itself is a thin proxy to api.helloverra.com -- detection runs in
Verra's backend
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

# Pop SDK-reserved + runtime-only values so the TFY SDK doesn't see them.
for _runtime_only in ("TFY_API_KEY", "WRAPPER_API_KEY", "VERRA_KEY"):
    os.environ.pop(_runtime_only, None)

SERVICE_NAME = os.environ.get("TFY_SERVICE_NAME", "verra-guardrails-tfy")
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
    f"tfy-secret://<workspace>/{SERVICE_NAME}/wrapper-api-key",
)
VERRA_KEY_SECRET_FQN = os.environ.get(
    "VERRA_KEY_SECRET_FQN",
    f"tfy-secret://<workspace>/{SERVICE_NAME}/verra-key",
)
VERRA_API_BASE = os.environ.get("VERRA_API_BASE", "https://api.helloverra.com")

BUILD_REF = _build_ref()


def build_service() -> Service:
    return Service(
        name=SERVICE_NAME,
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
            "WRAPPER_API_KEY": WRAPPER_API_KEY_SECRET_FQN,
            "VERRA_KEY": VERRA_KEY_SECRET_FQN,
            "VERRA_API_BASE": VERRA_API_BASE,
            "PORT": "8000",
            "LOG_LEVEL": "info",
            "BUILD_REF": BUILD_REF,
        },
        resources=Resources(
            cpu_request=0.25,
            cpu_limit=1.0,
            memory_request=256,
            memory_limit=1024,
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
        "WRAPPER_API_KEY_SECRET_FQN": WRAPPER_API_KEY_SECRET_FQN,
        "VERRA_KEY_SECRET_FQN": VERRA_KEY_SECRET_FQN,
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
    # remote build was still in flight and later transitioned to BUILD_FAILED.
    # Verify post-deploy that the version we just submitted actually became active.
    if args.wait:
        app_fqn = f"{WORKSPACE_FQN}:{SERVICE_NAME}"
        app = get_application(app_fqn)
        if app.activeVersion != app.lastVersion:
            print(
                f"\nDEPLOY FAILED: lastVersion={app.lastVersion} but activeVersion={app.activeVersion}.",
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
    print(f"  debug          : {base}/debug/loaded-config")
    print(f"  scan-input     : {base}/scan-input")
    print(f"  redact-input   : {base}/redact-input")
    print(f"  scan-output    : {base}/scan-output")
    print(f"  redact-output  : {base}/redact-output")
    print(
        "\nNext: register each rail as its own Custom Guardrail Config in the TFY dashboard."
        "\nAuth = Custom Bearer Auth with WRAPPER_API_KEY."
    )
