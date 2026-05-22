# TrueFoundry Deployment Playbook

How to deploy the wrapper as a TFY Service via the Python SDK. The SDK has several footguns; this file documents every one we hit on a real build.

## The `deploy.py` template

```python
"""Deploy the wrapper to TrueFoundry as a Service."""

import argparse
import os

from dotenv import load_dotenv
from truefoundry.deploy import (
    Build,
    DockerFileBuild,
    HealthProbe,
    HttpProbe,
    Port,
    Resources,
    Service,
)

# IMPORTANT: override=True makes .env the source of truth at deploy time.
# Without it, a stale value from a prior `source .env` in this shell will
# silently win, and the deploy will inject the old value into the pod.
load_dotenv(override=True)

# The TFY SDK reserves TFY_API_KEY (paired with TFY_HOST) for ITS OWN auth.
# Our .env uses TFY_API_KEY for the runtime wrapper's auth to the gateway --
# different concept. Strip those local-dev values out of the process env
# before the SDK initializes, so it falls back to the `tfy login` session.
# The deployed pod still gets them, but injected via the secret references
# below, not from this script's environment.
for _runtime_only in ("TFY_API_KEY", "WRAPPER_API_KEY"):
    os.environ.pop(_runtime_only, None)


WORKSPACE_FQN = os.environ.get("TFY_WORKSPACE_FQN", "<cluster>:<workspace>")

# Cluster-configured host. Look at Integrations -> Clusters -> <cluster>.
# Two flavors:
#   - Wildcard / per-service subdomain  (e.g. <service>.<cluster>.tld)
#       -> set PUBLIC_HOST to your chosen subdomain, leave PUBLIC_PATH empty.
#   - Shared base domain (e.g. ml.<cluster>.tld)
#       -> set PUBLIC_HOST to the base domain AND set PUBLIC_PATH.
PUBLIC_HOST = os.environ.get("TFY_PUBLIC_HOST", "<service>.<cluster>.truefoundry.cloud")

# Port.path regex requires leading AND trailing `/`. Normalize so users
# don't have to remember.
_raw_path = os.environ.get("TFY_PUBLIC_PATH", "").strip()
if _raw_path:
    if not _raw_path.startswith("/"):
        _raw_path = "/" + _raw_path
    if not _raw_path.endswith("/"):
        _raw_path = _raw_path + "/"
PUBLIC_PATH = _raw_path or None

VENDOR_KEY_SECRET_FQN  = os.environ["VENDOR_KEY_SECRET_FQN"]
WRAPPER_KEY_SECRET_FQN = os.environ["WRAPPER_API_KEY_SECRET_FQN"]


def build_service() -> Service:
    return Service(
        name="<vendor>-guardrail-tfy",
        # CRITICAL: Service.image expects a Build wrapper, not a bare DockerFileBuild.
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
            "TFY_BASE_URL": os.environ["TFY_BASE_URL"],
            "TFY_API_KEY":  VENDOR_KEY_SECRET_FQN,    # secret reference
            "JUDGE_MODEL":  os.environ["JUDGE_MODEL"],
            "WRAPPER_API_KEY": WRAPPER_KEY_SECRET_FQN, # secret reference
            "PORT": "8000",
            "LOG_LEVEL": "info",
        },
        resources=Resources(
            cpu_request=0.25, cpu_limit=1.0,
            memory_request=512, memory_limit=2048,
            ephemeral_storage_request=512, ephemeral_storage_limit=2048,
        ),
        liveness_probe=HealthProbe(
            config=HttpProbe(path="/health", port=8000),
            initial_delay_seconds=20, period_seconds=30, failure_threshold=3,
        ),
        readiness_probe=HealthProbe(
            config=HttpProbe(path="/health", port=8000),
            initial_delay_seconds=10, period_seconds=10, failure_threshold=3,
        ),
        replicas=1,
    )


def _check_placeholders() -> None:
    """Fail loudly if any required value still contains `<...>` (an unfilled placeholder)."""
    fields = {
        "WORKSPACE_FQN": WORKSPACE_FQN,
        "PUBLIC_HOST": PUBLIC_HOST,
        "VENDOR_KEY_SECRET_FQN": VENDOR_KEY_SECRET_FQN,
        "WRAPPER_KEY_SECRET_FQN": WRAPPER_KEY_SECRET_FQN,
    }
    unfilled = [name for name, val in fields.items() if "<" in val or ">" in val]
    if unfilled:
        raise SystemExit(
            "Placeholder values still present in deploy.py for: " + ", ".join(unfilled)
            + "\nFix by editing .env or the defaults at the top of deploy.py."
            + "\nFind cluster host: Integrations -> Clusters -> <cluster>."
            + "\nFind workspace FQN: Workspaces -> <workspace> (or `tfy workspace list`)."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait", action="store_true", help="block until the deploy is healthy")
    args = parser.parse_args()
    _check_placeholders()
    service = build_service()
    service.deploy(workspace_fqn=WORKSPACE_FQN, wait=args.wait)
    base = f"https://{PUBLIC_HOST}{PUBLIC_PATH or ''}"
    print(f"Deployed. Endpoint: {base}")
    print(f"  /input:  {base}/input")
    print(f"  /output: {base}/output")
```

## The SDK footguns (in the order you hit them)

### 1. `Port(host=...)` validates against cluster-configured hosts

If you pass a placeholder host like `<service>.<tenant>.truefoundry.cloud`, you get:

```
ValidationError: Invalid value for `host`. A valid host must contain only
alphanumeric letters and hyphens.
```

**Fix**: Integrations → Clusters → \<cluster\> page lists the configured base domain(s). Use a real value from there.

### 2. Shared base domains need a `path`

If the cluster uses a shared base domain (e.g. `ml.<cluster>.tld`) instead of wildcards per service, deploys without a path fail with:

```
BadRequestException: Path must be provided for port since the host is a base domain
```

**Fix**: set `Port(path="/<service>/")`. The regex requires leading AND trailing slashes. Normalize in code (see template above).

### 3. `Service.image` needs a `Build` wrapper

Passing `DockerFileBuild` directly:

```
ValidationError: No match for discriminator 'type' and value 'dockerfile'
(allowed values: 'build', 'image')
```

**Fix**: `image=Build(build_spec=DockerFileBuild(dockerfile_path="./Dockerfile"))`.

### 4. `TFY_API_KEY` env-var collision

If your `.env` has `TFY_API_KEY` for the *application's* use (calling the gateway as the rail judge), `load_dotenv()` puts it in `os.environ`. The SDK then sees `TFY_API_KEY` set and refuses to use the `tfy login` session:

```
ValueError: TFY_HOST env must be set since TFY_API_KEY env is set.
Either set TFY_HOST or unset TFY_API_KEY and login.
```

**Fix**: pop runtime-only env vars from `os.environ` after `load_dotenv()`, before the SDK initializes. Template does this for `TFY_API_KEY` and `WRAPPER_API_KEY`.

### 5. `load_dotenv()` doesn't override pre-existing env vars

If the user previously did `source .env` and then edited `.env`, the shell still holds the old values. `load_dotenv()` defaults to `override=False`, so those stale values silently win.

**Fix**: always use `load_dotenv(override=True)` in deploy scripts. Makes `.env` the source of truth at deploy time.

### 6. `service.deploy(wait=True)` returns success on `BUILD_FAILED`

This is the worst footgun in the SDK. Both modes lie in distinct ways:

**Mode A — silent build failure.** `service.deploy(wait=True)` can return successfully while the remote build is still pushing layers and later transitions to `BUILD_FAILED`. The script then prints "Deployed. Endpoints:" and exits with code 0. The old image keeps serving. `/debug/loaded-config` returns the *old* routes (which usually match expectations) — digest-comparison alone won't catch this if the source code shape didn't change.

Observed real failure: image push to ACR hung after `#16 exporting to image`, ~88 minutes later the build container hit timeout and was `SIGTERM`'d (`exit status 143`), version went `BUILD_FAILED`. Deploy script exited 0 and printed success URLs.

**Mode B — stale cache.** TrueFoundry's remote build sometimes serves cached layers if it thinks nothing has changed. New code is in the image, but the digest comparison fails because cached layers preserved older bytes.

**Mitigation (both modes)**:

1. **The deploy script must verify post-deploy** that `activeVersion == lastVersion`:

   ```python
   from truefoundry.deploy import get_application
   # ... after service.deploy(workspace_fqn=..., wait=args.wait):
   if args.wait:
       app = get_application(f"{WORKSPACE_FQN}:{SERVICE_NAME}")
       if app.activeVersion != app.lastVersion:
           sys.exit(f"DEPLOY FAILED: lastVersion={app.lastVersion}, activeVersion={app.activeVersion}")
   ```

   The `deploy.py` files in this repo already do this. Don't remove it.

2. **Set `BUILD_REF` in `Service.env`** (git short SHA, `+ "-dirty"` if working tree is unclean) and surface it as `wrapper_version` in `/debug/loaded-config`. Then "is the new code live" becomes a one-line curl + string compare, not a multi-file SHA diff.

3. **For Mode B specifically**: force a rebuild by touching `Dockerfile` (or any file in the build context) and redeploying.

### 7. ACR image push can hang for fat images and exceed the build-pod timeout

Observed once on `guardrails-ai-tfy` (image ~6.8 GB). Build log:

```
#16 exporting to image
#16 exporting layers ... done
#16 pushing layers          ← never progresses
... [88 minutes of silence] ...
Error: exit status 143      ← buildkit SIGTERM'd
tar: invalid tar magic       ← retry tried to re-download source tarball, gone
Updating build status to FAILED
```

The base image, `nltk_data`, hub validator weights (toxic-language: ~45 MB), and the full uv-installed Python tree push to >5 GB and saturate slow ACR egress paths. The retry mechanism fails its own re-download because the source tarball was cleaned up after the first attempt died.

**Mitigation, in order**:

1. **Retry once.** The push hang is usually transient ACR slowness, not a code bug. The second attempt typically reuses the cached lower layers and only needs to push the small upper layers.
2. **If it persists, slim the image.** Biggest wins: don't bake HF/spaCy/nltk model weights into the image (download at first run instead); multi-stage build that drops the `apt`, `pip`, and uv caches; strip dev deps from the runtime stage.
3. **Don't rely on `--wait`'s success.** Combined with footgun #6, the deploy can look successful from the script side while the cluster is still on the old image. Always verify `activeVersion == lastVersion` post-deploy.

## Secrets

Create two secrets in the TFY dashboard under **Platform → Secrets → + Secret Group `<vendor>-guardrail-tfy`**:

| Secret name | Value |
|---|---|
| `vendor-key` (or whatever the vendor calls its auth credential) | Used by the wrapper to call the vendor's service or the TFY gateway as the rail judge. |
| `wrapper-api-key` | A random string. The TFY gateway sends this as `Authorization: Bearer ...` when calling the wrapper. Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. |

Copy each secret's FQN (format `tfy-secret://<workspace>/<group>/<name>`) and put it in `.env`:

```
VENDOR_KEY_SECRET_FQN=tfy-secret://<workspace>/<vendor>-guardrail-tfy/vendor-key
WRAPPER_API_KEY_SECRET_FQN=tfy-secret://<workspace>/<vendor>-guardrail-tfy/wrapper-api-key
```

**Critical**: the `wrapper-api-key` secret's value must be the same string you configure in the dashboard's "Custom Bearer Auth" field on the guardrail config. If they don't match, the gateway calls the wrapper and gets 401, and `Fail on error: true` (which you must have set) returns "guardrail check failed" to the caller. From the outside it looks like a wiring bug; from inside it's a value mismatch.

## Authentication patterns

### `tfy login` for the SDK
```bash
tfy login
# Successfully logged in to 'https://<tenant>.truefoundry.cloud' as '<email>'
```

This stores a session locally. The SDK uses it unless `TFY_API_KEY`+`TFY_HOST` are present in env (which is why we pop them).

### TFY API keys for runtime
For the wrapper's own LLM calls back through the gateway, use a TFY API key (a JWT). Generate one in the dashboard under Settings → Personal Access Tokens or Service Accounts. Service-account tokens are preferred for production wrappers because they don't expire when the issuing user leaves.

## Public URL layout

For a cluster with shared base domain `ml.<cluster>.truefoundry.cloud` and a service deployed at `path: /<service>/`, the public URLs are:

```
https://ml.<cluster>.truefoundry.cloud/<service>/health
https://ml.<cluster>.truefoundry.cloud/<service>/input
https://ml.<cluster>.truefoundry.cloud/<service>/output
https://ml.<cluster>.truefoundry.cloud/<service>/debug/loaded-config
```

The TFY ingress strips the `/<service>/` prefix before forwarding to the container, so FastAPI sees `/input` etc. If you ever see 404s instead of 200s on `/health`, the ingress isn't stripping — set `root_path=/<service>` on the FastAPI app or `UVICORN_ROOT_PATH=/<service>` in the env.

## Iteration without redeploying

`uvicorn --reload` watches `.py` files only, NOT YAML or other config files. So:

- Editing `app/*.py` → uvicorn reloads automatically.
- Editing `config/*.yml` → in-memory state stays stale. Either restart uvicorn or `touch app/main.py` to force a reload.

This matters during local dev. On the deployed pod, every change requires a redeploy.

## Verify the deploy actually replaced the pod

Always, every time:

```bash
curl -sS https://<deployed>/<path>/debug/loaded-config \
  -H "Authorization: Bearer $WRAPPER_API_KEY" | jq

# Compare digests
shasum -a 256 config/prompts.yml   # or whatever config files matter
```

If digests don't match, the deploy didn't take. Most common cause: image build cache or rollout still in progress. Less common: you pushed to git but `deploy.py` packages local files via LocalSource — confirm you're deploying from the same working directory you edited.
