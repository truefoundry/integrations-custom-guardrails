"""Deploy the test-only stub SQL MCP server to TrueFoundry.

Usage (from this directory, with the wrapper's .venv or any env that has truefoundry):
    TFY_WORKSPACE_FQN=<cluster>:<workspace> TFY_PUBLIC_HOST=<host> TFY_PUBLIC_PATH=/stub-sql-mcp/ \
        python deploy.py --wait

The MCP endpoint is <public base>/mcp.
"""

import argparse
import os

from truefoundry.deploy import Build, DockerFileBuild, HealthProbe, HttpProbe, Port, Resources, Service

os.environ.pop("TFY_API_KEY", None)

SERVICE_NAME = os.environ.get("TFY_STUB_SERVICE_NAME", "stub-sql-mcp")
WORKSPACE_FQN = os.environ.get("TFY_WORKSPACE_FQN", "<cluster>:<workspace>")
PUBLIC_HOST = os.environ.get("TFY_PUBLIC_HOST", "ml.<cluster>.truefoundry.cloud")
PUBLIC_PATH = os.environ.get("TFY_PUBLIC_PATH", "").strip() or None
if PUBLIC_PATH:
    PUBLIC_PATH = f"/{PUBLIC_PATH.strip('/')}/"


def build_service() -> Service:
    return Service(
        name=SERVICE_NAME,
        image=Build(build_spec=DockerFileBuild(dockerfile_path="./Dockerfile")),
        ports=[
            Port(port=8000, host=PUBLIC_HOST, **({"path": PUBLIC_PATH} if PUBLIC_PATH else {}), protocol="TCP", expose=True)
        ],
        env={"PORT": "8000"},
        resources=Resources(cpu_request=0.1, cpu_limit=0.5, memory_request=128, memory_limit=512),
        liveness_probe=HealthProbe(config=HttpProbe(path="/health", port=8000), initial_delay_seconds=10, period_seconds=30),
        readiness_probe=HealthProbe(config=HttpProbe(path="/health", port=8000), initial_delay_seconds=5, period_seconds=10),
        replicas=1,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()
    if any("<" in v for v in (WORKSPACE_FQN, PUBLIC_HOST)):
        raise SystemExit("Set TFY_WORKSPACE_FQN and TFY_PUBLIC_HOST first.")
    build_service().deploy(workspace_fqn=WORKSPACE_FQN, wait=args.wait)
    print(f"\nMCP endpoint: https://{PUBLIC_HOST}{(PUBLIC_PATH or '').rstrip('/')}/mcp")
