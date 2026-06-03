"""Deploy the Knostic guardrail wrapper to TrueFoundry as a Service.

Usage:
    pip install -U "truefoundry"
    tfy login
    # Fill in .env (or edit defaults below), then:
    python deploy.py --wait
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


load_dotenv(override=True)

for _runtime_only in ("KNOSTIC_API_KEY", "WRAPPER_API_KEY", "TFY_API_KEY"):
    os.environ.pop(_runtime_only, None)

WORKSPACE_FQN = os.environ.get("TFY_WORKSPACE_FQN", "<workspace>:<name>")
PUBLIC_HOST = os.environ.get("TFY_PUBLIC_HOST", "ml.<cluster>.truefoundry.cloud")
_raw_path = os.environ.get("TFY_PUBLIC_PATH", "").strip()
if _raw_path:
    if not _raw_path.startswith("/"):
        _raw_path = "/" + _raw_path
    if not _raw_path.endswith("/"):
        _raw_path = _raw_path + "/"
PUBLIC_PATH = _raw_path or None

KNOSTIC_API_KEY_SECRET_FQN = os.environ.get(
    "KNOSTIC_API_KEY_SECRET_FQN",
    "tfy-secret://<workspace>/knostic-guardrails-tfy/knostic-api-key",
)
WRAPPER_API_KEY_SECRET_FQN = os.environ.get(
    "WRAPPER_API_KEY_SECRET_FQN",
    "tfy-secret://<workspace>/knostic-guardrails-tfy/wrapper-api-key",
)

KNOSTIC_API_BASE = os.environ.get("KNOSTIC_API_BASE", "https://api.knostic.ai")
KNOSTIC_INSPECT_PATH = os.environ.get("KNOSTIC_INSPECT_PATH", "/v1/guardrails/inspect")
KNOSTIC_SANITIZE_PATH = os.environ.get("KNOSTIC_SANITIZE_PATH", "/v1/guardrails/sanitize")
KNOSTIC_POLICY_ID = os.environ.get("KNOSTIC_POLICY_ID", "")

BUILD_REF = _build_ref()


def build_service() -> Service:
    env: dict[str, str] = {
        "KNOSTIC_API_KEY": KNOSTIC_API_KEY_SECRET_FQN,
        "WRAPPER_API_KEY": WRAPPER_API_KEY_SECRET_FQN,
        "KNOSTIC_API_BASE": KNOSTIC_API_BASE,
        "KNOSTIC_INSPECT_PATH": KNOSTIC_INSPECT_PATH,
        "KNOSTIC_SANITIZE_PATH": KNOSTIC_SANITIZE_PATH,
        "PORT": "8000",
        "LOG_LEVEL": "info",
        "BUILD_REF": BUILD_REF,
    }
    if KNOSTIC_POLICY_ID:
        env["KNOSTIC_POLICY_ID"] = KNOSTIC_POLICY_ID

    return Service(
        name="knostic-guardrails-tfy",
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
        "KNOSTIC_API_KEY_SECRET_FQN": KNOSTIC_API_KEY_SECRET_FQN,
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

    if args.wait:
        app_fqn = f"{WORKSPACE_FQN}:knostic-guardrails-tfy"
        app = get_application(app_fqn)
        if app.activeVersion != app.lastVersion:
            print(
                f"\nDEPLOY FAILED: lastVersion={app.lastVersion} but activeVersion={app.activeVersion}.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"\nVerified: activeVersion == lastVersion == {app.activeVersion}, build_ref={BUILD_REF}")

    base = f"https://{PUBLIC_HOST}{(PUBLIC_PATH or '').rstrip('/')}"
    print(f"\nDeployed. Endpoints:")
    print(f"  health                   : {base}/health")
    print(f"  input validate URL       : {base}/knostic-prompt-inspect-input")
    print(f"  output validate URL      : {base}/knostic-prompt-inspect-output")
    print(f"  input mutate URL         : {base}/knostic-prompt-sanitize-input")
    print(f"  output mutate URL        : {base}/knostic-prompt-sanitize-output")
    print(f"  debug                    : {base}/debug/loaded-config")
    print(
        "\nNext: register each rail as its own Custom Guardrail Config in the TFY dashboard.\n"
        "Validate rails: Operation = Validate; Mutate rails: Operation = Mutate.\n"
        "Auth = Custom Bearer Auth with wrapper-api-key.\n"
        "With gateway commit a1c551be+, Fail on error: false is the correct default."
    )
