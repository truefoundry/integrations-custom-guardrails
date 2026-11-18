"""Deploy the Bigeye guardrail wrapper to TrueFoundry as a Service.

Usage:
    pip install -U "truefoundry"
    tfy login
    # Fill in .env, then:
    python deploy.py --wait

The service holds no Bigeye credentials: each guardrail config's Bearer token
is the Bigeye API key, forwarded per request. See the six TFY SDK footguns in
docs/add-a-new-integration.md.
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

# override=True makes .env the source of truth at deploy time. Without it,
# a stale value from a prior `source .env` in this shell would silently win.
load_dotenv(override=True)

# The TFY SDK reserves TFY_API_KEY for its own auth; BIGEYE_API_KEY is for the
# smoke test only and must never reach the service.
for _runtime_only in ("TFY_API_KEY", "BIGEYE_API_KEY"):
    os.environ.pop(_runtime_only, None)

SERVICE_NAME = os.environ.get("TFY_SERVICE_NAME", "bigeye-guardrails-tfy")

WORKSPACE_FQN = os.environ.get("TFY_WORKSPACE_FQN", "<cluster>:<workspace>")
PUBLIC_HOST = os.environ.get("TFY_PUBLIC_HOST", "ml.<cluster>.truefoundry.cloud")

_raw_path = os.environ.get("TFY_PUBLIC_PATH", "").strip()
if _raw_path:
    if not _raw_path.startswith("/"):
        _raw_path = "/" + _raw_path
    if not _raw_path.endswith("/"):
        _raw_path = _raw_path + "/"
PUBLIC_PATH = _raw_path or None

BIGEYE_ALLOWED_HOSTS = os.environ.get("BIGEYE_ALLOWED_HOSTS", "*.bigeye.com")
LOG_PAYLOADS = os.environ.get("LOG_PAYLOADS", "false")


def _build_ref() -> str:
    """Short identifier for this deploy. Surfaces as `wrapper_version` on /debug/loaded-config.

    Prefer the working-tree git SHA so the pod's reported version matches the
    source. Fall back to an epoch tag if git is unavailable. NOTE: an unclean
    working tree still maps to the committed SHA -- be explicit about local edits.
    """
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
            "PORT": "8000",
            "LOG_LEVEL": "info",
            "BUILD_REF": BUILD_REF,
            "BIGEYE_ALLOWED_HOSTS": BIGEYE_ALLOWED_HOSTS,
            "LOG_PAYLOADS": LOG_PAYLOADS,
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
            initial_delay_seconds=10,
            period_seconds=30,
            failure_threshold=3,
        ),
        readiness_probe=HealthProbe(
            config=HttpProbe(path="/health", port=8000),
            initial_delay_seconds=5,
            period_seconds=10,
            failure_threshold=3,
        ),
        replicas=1,
    )


def _check_placeholders() -> None:
    fields = {
        "SERVICE_NAME": SERVICE_NAME,
        "WORKSPACE_FQN": WORKSPACE_FQN,
        "PUBLIC_HOST": PUBLIC_HOST,
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
    parser.add_argument("--wait", action="store_true", help="block until the deploy is healthy")
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
            print(f"Check build logs in the TFY dashboard under application id {app.id}.", file=sys.stderr)
            sys.exit(1)
        print(f"\nVerified: activeVersion == lastVersion == {app.activeVersion}, build_ref={BUILD_REF}")

    base = f"https://{PUBLIC_HOST}{(PUBLIC_PATH or '').rstrip('/')}"
    print(f"\nDeployed. Endpoint: {base}")
    print(f"  health  : {base}/health")
    print(f"  debug   : {base}/debug/loaded-config")
    print(f"  rail    : {base}/mcp-pre-invoke")
    print(
        "\nNext: register the guardrail in TrueFoundry (README, \"Register in TrueFoundry\"; example YAML in\n"
        "devtools/gateway-examples/). Operation = Validate; Bearer token = the Bigeye API key;\n"
        "enforcing strategy = enforce_but_ignore_on_error."
    )
