"""
Deploy the DeepKeep guardrail wrapper as a TrueFoundry Service.

Reference: https://www.truefoundry.com/docs/deploy-first-service
Ports/domains: https://www.truefoundry.com/docs/define-ports-and-domains

Auth + workspace come from .env:
  TFY_HOST, TFY_API_KEY, TFY_WORKSPACE_FQN
Optional:
  TFY_SERVICE_HOST — public hostname for Port(expose=True). If omitted, we
  try to derive one from the workspace cluster's base domains.
"""

import argparse
import logging
import os
import re
from urllib.parse import urlparse

from dotenv import load_dotenv
from truefoundry import login
from truefoundry.deploy import Build, LocalSource, Port, PythonBuild, Resources, Service
from truefoundry.deploy.lib.clients.servicefoundry_client import ServiceFoundryServiceClient
from truefoundry.deploy.lib.dao.workspace import get_workspace_by_fqn

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("deploy")

SERVICE_NAME = "deepkeep-guardrail-wrapper"


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name) or default
    if not value:
        raise SystemExit(f"Missing required env var: {name}")
    return value.strip()


def _tfy_login() -> None:
    host = _env("TFY_HOST").rstrip("/")
    api_key = _env("TFY_API_KEY")
    logger.info("Logging into TrueFoundry host=%s", host)
    login(host=host, api_key=api_key, relogin=True)


def _workspace_name(workspace_fqn: str) -> str:
    # FQN format: <tenant/cluster>:<workspace>
    return workspace_fqn.split(":")[-1]


def _cluster_base_domains(workspace_fqn: str) -> list[str]:
    """Fetch base domains configured on the workspace's cluster."""
    import re

    domain_re = re.compile(r"^(?:\*\.)?(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}$")

    def walk(obj, found: set[str]) -> None:
        if isinstance(obj, str):
            s = obj.strip()
            if domain_re.match(s):
                found.add(s.lstrip("*.").rstrip("/"))
        elif isinstance(obj, dict):
            for v in obj.values():
                walk(v, found)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, found)

    client = ServiceFoundryServiceClient()
    ws = get_workspace_by_fqn(workspace_fqn, client=client)
    data = ws.dict() if hasattr(ws, "dict") else (ws if isinstance(ws, dict) else {})
    cluster_id = data.get("clusterId") or data.get("cluster_id")
    if not cluster_id:
        return []

    cluster = client.get_cluster(cluster_id)
    found: set[str] = set()
    walk(cluster, found)

    # Prefer app ingress domains (ml.*) over control-plane / notebook hosts.
    preferred = sorted(
        d
        for d in found
        if d.startswith("ml.") or ".ml." in d
    )
    if preferred:
        # Prefer shortest ml.* base (exclude notebook.ml.*)
        preferred.sort(key=lambda d: (d.count("."), len(d)))
        return preferred

    # Fallback: any non-notebook domain
    other = sorted(d for d in found if "notebook" not in d and "accelerator" not in d)
    return other or sorted(found)


def _resolve_service_host(workspace_fqn: str) -> str:
    explicit = os.environ.get("TFY_SERVICE_HOST", "").strip()
    if explicit:
        return explicit

    ws_name = _workspace_name(workspace_fqn)
    # Common TrueFoundry convention: <service>-<workspace>.<base-domain>
    domains = _cluster_base_domains(workspace_fqn)
    if domains:
        host = f"{SERVICE_NAME}-{ws_name}.{domains[0]}"
        logger.info("Derived service host from cluster domain: %s", host)
        return host

    # Fallback: derive a host-ish name from TFY_HOST (may fail validation if
    # the cluster doesn't use this domain — set TFY_SERVICE_HOST explicitly).
    parsed = urlparse(_env("TFY_HOST"))
    tfy_host = parsed.netloc or parsed.path
    # e.g. tfy-eo.truefoundry.cloud -> often apps live under a sibling domain
    fallback = f"{SERVICE_NAME}-{ws_name}.{tfy_host}"
    logger.warning(
        "Could not read cluster domains; using fallback host %s. "
        "If deploy fails, set TFY_SERVICE_HOST in .env to the domain from the UI dropdown.",
        fallback,
    )
    return fallback


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--workspace_fqn",
        default=None,
        help="Override TFY_WORKSPACE_FQN from .env",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Override TFY_SERVICE_HOST / auto-derived public hostname",
    )
    args = parser.parse_args()

    _tfy_login()

    workspace_fqn = (args.workspace_fqn or _env("TFY_WORKSPACE_FQN")).strip()
    service_host = (args.host or _resolve_service_host(workspace_fqn)).strip()
    # Port.host must be a bare hostname (no scheme)
    service_host = re.sub(r"^https?://", "", service_host).rstrip("/")

    dk_api_key = os.environ.get("DEEPKEEP_API_KEY_TFY_SECRET") or _env("DEEPKEEP_API_KEY")
    dk_base_url = _env("DEEPKEEP_BASE_URL", "https://api.poc2.aws.deepkeep.ai")
    input_fw = _env("DEEPKEEP_INPUT_FIREWALL_ID")
    output_fw = os.environ.get("DEEPKEEP_OUTPUT_FIREWALL_ID") or input_fw

    logger.info("Deploying %s to workspace=%s host=%s", SERVICE_NAME, workspace_fqn, service_host)

    service = Service(
        name=SERVICE_NAME,
        image=Build(
            build_source=LocalSource(project_root_path="./", local_build=False),
            build_spec=PythonBuild(
                python_version="3.11",
                build_context_path="./",
                requirements_path="requirements.txt",
                command="uvicorn main:app --host 0.0.0.0 --port 8080",
            ),
        ),
        resources=Resources(
            cpu_request=0.2,
            cpu_limit=0.5,
            memory_request=256,
            memory_limit=512,
        ),
        env={
            "DEEPKEEP_BASE_URL": dk_base_url,
            "DEEPKEEP_API_KEY": dk_api_key,
            "DEEPKEEP_INPUT_FIREWALL_ID": input_fw,
            "DEEPKEEP_OUTPUT_FIREWALL_ID": output_fw,
        },
        ports=[
            Port(
                port=8080,
                protocol="TCP",
                expose=True,
                app_protocol="http",
                host=service_host,
            )
        ],
        replicas=1.0,
    )

    deployment = service.deploy(workspace_fqn=workspace_fqn, wait=False)
    print(deployment)


if __name__ == "__main__":
    main()
