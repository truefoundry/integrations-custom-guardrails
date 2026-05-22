# Adding a new custom-guardrail integration

Step-by-step playbook. Estimated time: 1–2 days for a vendor with a clean API; longer if the vendor's quirks require investigation.

## Prerequisites

- A TrueFoundry workspace you can deploy services into.
- The vendor's credentials/API key (or whatever auth they require).
- A cluster with a configured base host (visible at **Integrations → Clusters → \<cluster\>** in the TFY dashboard).
- This repo cloned locally.
- Read [`gateway-contract.md`](gateway-contract.md) — that's the contract you're conforming to.
- Skim the two existing integrations (`integrations/nemo/`, `integrations/guardrails-ai/`) — they're worked examples of the pattern.

For deeper guidance: the [`.claude/skills/truefoundry-custom-guardrail/`](../.claude/skills/truefoundry-custom-guardrail/) skill is the full 7-phase playbook. Read `SKILL.md` and the four reference files in `references/` if you're integrating a non-trivial vendor.

## Phase 0 — Validate the vendor locally

Before writing wrapper code, prove the vendor behaves the way you expect.

```bash
# Outside the repo, in a scratch dir
mkdir -p /tmp/<vendor>-smoke && cd /tmp/<vendor>-smoke
python3 -m venv .venv
.venv/bin/pip install <vendor-sdk> jupyter ipykernel python-dotenv

# Write a notebook that:
# 1. Authenticates to the vendor
# 2. Hits each capability you'll claim in v1 (input validation, output validation, mutation)
# 3. Captures latency, response shape, edge cases
# 4. Has a Findings markdown cell with concrete numbers + decisions
```

The Findings cell drives the next phase: what verdict signal does the vendor return (boolean? score? structured violation list?), what's the latency budget, does it need an LLM judge, and is the SDK stable.

## Phase 1 — Scaffold the integration directory

```bash
cd path/to/tfy-custom-guardrails    # repo root
cp -r integrations/_template integrations/<vendor>
cd integrations/<vendor>
```

Edit these files in order:

1. **`README.md`** — overview of what the vendor does, which rails you're shipping, endpoint URLs.
2. **`requirements.txt`** — add the vendor SDK. Pin loosely (`>=x.y,<NEXT_MAJOR`). If the vendor's PyPI is unreliable (Guardrails AI is quarantined as of this writing), pin to a GitHub tag.
3. **`entities.py`** — already in the template. Don't change unless the gateway contract changed; see [`gateway-contract.md`](gateway-contract.md).
4. **`guardrail/<rail_name>_input.py`** — your input rail handler. One file per rail per direction.
5. **`guardrail/<rail_name>_output.py`** — your output rail handler. Skip if your vendor is input-only.
6. **`main.py`** — register your rail routes via `app.add_api_route(...)`. Edit the imports and the `RAIL_ROUTES` map (if using the same pattern as `guardrails-ai`).
7. **`Dockerfile`** — most vendors work with the template's Dockerfile unchanged. Add `apt-get install` lines if your vendor needs system deps (`git`, `build-essential`, etc.). Add `setup.py` invocation if your vendor requires build-time installs (Guardrails Hub validators, NVIDIA NGC, etc.).
8. **`deploy.py`** — update `name`, `WORKSPACE_FQN`, `PUBLIC_HOST`, `TFY_PUBLIC_PATH`, env vars, secret references.
9. **`tests/test_smoke.py`** — copy the test cases from `_template` and adapt to your rail names. Required cases listed below.

### Per-rail handler shape

```python
# guardrail/<rail_name>_input.py
from guardrails import Guard   # or whatever your vendor exposes
from <vendor> import <Validator>

from entities import InputGuardrailRequest, ValidateGuardrailResponse
from guardrail._helpers import last_user_text


# Module-scope: load expensive state once at import time
guard = Guard().use(<Validator>(on_fail="exception"))


def <rail_name>_input(request: InputGuardrailRequest) -> ValidateGuardrailResponse:
    user_msg = last_user_text(request.requestBody.get("messages") or [])
    if user_msg is None:
        return ValidateGuardrailResponse(verdict=True)   # short-circuit on empty
    try:
        guard.validate(user_msg)
        return ValidateGuardrailResponse(verdict=True)
    except Exception as e:
        return ValidateGuardrailResponse(
            verdict=False,
            message=f"<Validator> (input): {str(e)[:300]}",
        )
```

For vendors with **heavy init** (NeMo's `RailsConfig.from_path` + `LLMRails` construction), use a module-import singleton in `guardrail/_<vendor>_runner.py` and have all per-rail files import the same runner. See `integrations/nemo/guardrail/_nemo_runner.py` for the canonical pattern.

## Phase 2 — Tests

Required smoke cases (copy from `_template/tests/test_smoke.py` and adapt):

- `test_health` — GET `/health` returns 200.
- `test_missing_bearer_returns_401` — POST `/<rail>` without Authorization → 401.
- `test_wrong_bearer_returns_401` — POST with wrong token → 401.
- `test_no_user_message_passes_through` — system-only history → 200 + verdict=true.
- `test_no_assistant_message_passes_through` — empty choices → 200 + verdict=true.
- `test_debug_loaded_config` — GET `/debug/loaded-config` returns the expected route list.
- `test_<benign_case>_passes` — benign input → 200 + verdict=true.
- `test_<violating_case>_blocks` — violating input → 200 + verdict=false with the validator name in `message`.

Mark live-vendor tests with `@pytest.mark.skipif(...)` so the suite runs in CI without secrets. Module-scoped `TestClient` fixture so vendor init runs once per test module.

Run:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env  # fill in
.venv/bin/pytest -v tests/
```

## Phase 3 — Deploy to TrueFoundry

```bash
.venv/bin/pip install -U truefoundry
tfy login
.venv/bin/python deploy.py --wait
```

If you hit any of the six TFY SDK footguns, see [`.claude/skills/truefoundry-custom-guardrail/references/deployment-playbook.md`](../.claude/skills/truefoundry-custom-guardrail/references/deployment-playbook.md). Most common ones:

- `load_dotenv(override=True)` — without it, stale shell vars win.
- Pop `TFY_API_KEY` / vendor tokens from `os.environ` before SDK init.
- `Service.image` needs `Build(build_spec=DockerFileBuild(...))`, not bare `DockerFileBuild`.
- `Port(host=...)` validates against cluster-configured hosts.
- Path needs leading + trailing `/`.

## Phase 4 — Register in the dashboard

**AI Gateway → Guardrails → + Add New Guardrails Group**. Name it `<vendor>-<bundle>` (e.g. `nemo-self-check`, `guardrails-ai`).

For each rail, add a Custom Guardrail Config:
- **Name** (this is the per-rail config name, e.g. `detect-pii-input`).
- **URL**: the per-rail endpoint on the deployed wrapper (e.g. `https://<host>/<path>/<rail>-input`).
- **Operation**: `Validate` unless your vendor mutates content.
- **Auth Data**: Custom Bearer Auth with your `wrapper-api-key` secret value.
- **Headers**: empty.
- **Config**: `{}`.
- **Fail on error**: `false` (correct default on the post-`a1c551be` gateway).

### How the dashboard names map to the gateway selector

The **group name** + **config name** you pick above are what clients reference in the `X-TFY-GUARDRAILS` header to pin specific rails on a request:

```python
extra_headers = {"X-TFY-GUARDRAILS": json.dumps({
    "llm_input_guardrails":  ["<group-name>/<input-config-name>"],
    "llm_output_guardrails": ["<group-name>/<output-config-name>"],
})}
```

This `<group>/<config>` selector is the only "namespace" concept the gateway exposes — it lives in dashboard metadata, **not** in the wrapper URL. You can register the same wrapper URL under multiple groups (e.g. once under `safety-rails`, once under `experiments`) with different config names; clients pick which version to apply per request.

Two URL shape choices are independent of this:

1. **Path-prefix routing** (default for shared base domains): `https://ml.<cluster>.tld/<service>/<rail>-input`.
2. **Per-service subdomain** (only if the cluster has a wildcard host configured): `https://<service>.<cluster>.tld/<rail>-input` — set `Port(host=...)` with no path.

Both work with the dashboard registration. Pick path-prefix unless you have a specific reason (per-service mTLS, separate origin per wrapper, etc.).

## Phase 5 — Verify end-to-end

Three layers, in order. Don't skip layers.

1. **Wrapper alone**: direct POST to the deployed wrapper's `/health` and `/<rail>-input` with the bearer token. Confirm the wrapper returns the right HTTP shape.
2. **`/debug/loaded-config`**: confirm the pod has the rails/config you expect. Compare digests against local files.
3. **Through the gateway**: send the standard test prompts (one benign, several discriminating violations) through the TFY gateway with the `X-TFY-GUARDRAILS` header. Confirm the gateway is calling the wrapper and honoring the verdict.

Use **discriminating test prompts** — prompts that the underlying model would happily comply with on its own, where rail-on vs rail-off produces visibly different output. Without these, your guardrail will look like it works when actually the model is doing all the work.

See [`guardrail-test-phrases.md`](guardrail-test-phrases.md) for the canonical demo prompt list.

## Phase 6 — Document

Four artifacts in `integrations/<vendor>/docs/`:

1. **`README.md`** at integration root — quickstart for contributors. Endpoints, local run, tests, deploy, dashboard wiring.
2. **`docs/DESIGN.md`** — architecture, verdict mapping, decisions, gotchas, failure modes, future work.
3. **`docs/blog-<vendor>.md`** — technical blog draft for `truefoundry.com/blog/<vendor>-integration`. Use the `truefoundry-integration-blog` skill if available (strict style: no comma-grouping, no marketing language, architecture-first).
4. **`docs/public-docs-<vendor>.md`** — end-user setup guide for `truefoundry.com/docs/ai-gateway/<vendor>`. Tutorial style: prerequisites, step-by-step, test, troubleshooting, known limitations, reference table.

## Phase 7 — Update repo-level tracking

1. Add a row to the "Current integrations" table in the top-level [`README.md`](../README.md).
2. Add prompts (if any are new and useful) to [`guardrail-test-phrases.md`](guardrail-test-phrases.md).
3. Update whichever team-tracker doc lives outside this repo (Confluence, Linear, etc.). Project tracking is out of scope for the repo itself.

## Hard rules (don't break these)

1. **No vendor SDK code in the gateway runtime.** Vendor logic stays behind the wrapper's HTTP boundary.
2. **`Fail on error: false`** on every Custom Guardrail Config (post-`a1c551be` gateway). Use `true` only for safety-critical rails.
3. **Route any LLM calls the vendor needs back through the TFY gateway**, not directly to a provider. Unified observability.
4. **Always include `/debug/loaded-config`**. Saves hours per deploy.
5. **Verify deploys with the debug digest before assuming new code is live.** TFY's image build cache has surprised every integrator at least once.
6. **Test with discriminating prompts only.** A prompt the underlying model would refuse on its own teaches you nothing.
7. **Don't commit secrets.** `.gitignore` already covers `.env`, `.venv/`, `__pycache__/`, `.guardrails/`. Verify before pushing.
