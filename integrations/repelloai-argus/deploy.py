"""Deploy the RepelloAI Argus guardrail wrapper to TrueFoundry as a Service.

Builds the image from the Dockerfile, exposes port 8000 over HTTPS, wires the
Argus and wrapper secrets, and probes /health.

Usage:
    pip install -U truefoundry
    tfy login
    # Fill in .env (or edit the defaults below), then:
    python deploy.py --wait

The wrapper verifies its credentials and asset ID at startup, so a bad secret
reference fails the deploy instead of producing a guardrail that allows
everything.
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

SERVICE_NAME = "repelloai-argus-guardrails-tfy"


def _build_ref() -> str:
    """Short identifier for this deploy. Surfaces as `wrapper_version` on /debug/loaded-config."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        dirty = subprocess.call(
            ["git", "diff", "--quiet", "--exit-code"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return f"{sha}{'-dirty' if dirty else ''}"
    except Exception:
        return f"deploy-{int(time.time())}"


# override=True makes .env the source of truth at deploy time.
load_dotenv(override=True)

# Strip local-dev-only secrets so the SDK uses `tfy login` session auth.
for _runtime_only in ("TFY_API_KEY", "ARGUS_API_KEY", "WRAPPER_API_KEY"):
    os.environ.pop(_runtime_only, None)

# ---------------------------------------------------------------------------
# Placeholders — replace via .env or edit defaults before deploying.
# ---------------------------------------------------------------------------

WORKSPACE_FQN = os.environ.get("TFY_WORKSPACE_FQN", "<workspace>:<name>")
PUBLIC_HOST = os.environ.get("TFY_PUBLIC_HOST", "ml.<cluster>.truefoundry.cloud")
_raw_path = os.environ.get("TFY_PUBLIC_PATH", "").strip()
if _raw_path:
    if not _raw_path.startswith("/"):
        _raw_path = "/" + _raw_path
    if not _raw_path.endswith("/"):
        _raw_path = _raw_path + "/"
PUBLIC_PATH = _raw_path or None

ARGUS_API_KEY_SECRET_FQN = os.environ.get(
    "ARGUS_API_KEY_SECRET_FQN",
    f"tfy-secret://<workspace>/{SERVICE_NAME}/argus-api-key",
)
WRAPPER_API_KEY_SECRET_FQN = os.environ.get(
    "WRAPPER_API_KEY_SECRET_FQN",
    f"tfy-secret://<workspace>/{SERVICE_NAME}/wrapper-api-key",
)

ARGUS_API_BASE = os.environ.get("ARGUS_API_BASE", "https://argusapi.repello.ai/sdk/v1")
ARGUS_ASSET_ID = os.environ.get("ARGUS_ASSET_ID", "")
ARGUS_TIMEOUT_S = os.environ.get("ARGUS_TIMEOUT_S", "6.0")
ARGUS_MAX_TEXT_CHARS = os.environ.get("ARGUS_MAX_TEXT_CHARS", "20000")

BUILD_REF = _build_ref()


def build_service() -> Service:
    env = {
        "ARGUS_API_KEY": ARGUS_API_KEY_SECRET_FQN,
        "WRAPPER_API_KEY": WRAPPER_API_KEY_SECRET_FQN,
        "ARGUS_API_BASE": ARGUS_API_BASE,
        "ARGUS_ASSET_ID": ARGUS_ASSET_ID,
        "ARGUS_TIMEOUT_S": ARGUS_TIMEOUT_S,
        "ARGUS_MAX_TEXT_CHARS": ARGUS_MAX_TEXT_CHARS,
        "PORT": "8000",
        "LOG_LEVEL": "info",
        "BUILD_REF": BUILD_REF,
    }
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
        env=env,
        resources=Resources(
            cpu_request=0.25,
            cpu_limit=1.0,
            memory_request=512,
            memory_limit=1024,
            ephemeral_storage_request=512,
            ephemeral_storage_limit=1024,
        ),
        liveness_probe=HealthProbe(
            config=HttpProbe(path="/health", port=8000),
            initial_delay_seconds=15,
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
        "WORKSPACE_FQN": WORKSPACE_FQN,
        "PUBLIC_HOST": PUBLIC_HOST,
        "ARGUS_API_KEY_SECRET_FQN": ARGUS_API_KEY_SECRET_FQN,
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
                    "Find workspace FQN at: Workspaces -> <name> (or `tfy workspace list`).",
                ]
            )
        )
    if not ARGUS_ASSET_ID:
        raise SystemExit(
            "ARGUS_ASSET_ID is empty. The wrapper verifies it against Argus at "
            "startup and will refuse to boot, so set it in .env before deploying."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait", action="store_true", help="block until the deploy is healthy")
    args = parser.parse_args()

    _check_placeholders()
    service = build_service()
    service.deploy(workspace_fqn=WORKSPACE_FQN, wait=args.wait)

    if args.wait:
        app_fqn = f"{WORKSPACE_FQN}:{SERVICE_NAME}"
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
                "If the pod is crash-looping, check its logs: the wrapper exits on missing "
                "env vars or an Argus asset ID that fails verification.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"\nVerified: activeVersion == lastVersion == {app.activeVersion}, build_ref={BUILD_REF}")

    base = f"https://{PUBLIC_HOST}{(PUBLIC_PATH or '').rstrip('/')}"
    print("\nDeployed. Endpoints:")
    print(f"  health              : {base}/health")
    print(f"  validate input      : {base}/validate-input     (Operation: Validate)")
    print(f"  validate output     : {base}/validate-output    (Operation: Validate)")
    print(f"  redact input        : {base}/redact-input       (Operation: Mutate)")
    print(f"  redact output       : {base}/redact-output      (Operation: Mutate)")
    print(f"  debug               : {base}/debug/loaded-config")
    print(
        "\nNext: register each rail you want as its own Custom Guardrail Config in the\n"
        "TFY dashboard, with the Operation shown above and Custom Bearer Auth using\n"
        "wrapper-api-key. Register one rail per direction: a Mutate rail returns a whole\n"
        "replacement body, so two rails on one hook means the second overwrites the first.\n"
        "Fail on error: true on the input rail, false on the output rail.\n"
        "Requires a gateway at or past commit a1c551be; older gateways read\n"
        "200 + verdict:false as 'passed'."
    )
