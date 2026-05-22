# Working dir context — tfy-custom-guardrails

Monorepo for TrueFoundry AI Gateway custom-guardrail integrations. Each `integrations/<vendor>/` is an independently-deployable FastAPI service that conforms to the gateway's [`docs/gateway-contract.md`](docs/gateway-contract.md). Two reference integrations ship in the repo today: `integrations/nemo/` (LLM-judged) and `integrations/guardrails-ai/` (local heuristic validators).

This file is the **starting point** for any Claude Code session opened in this directory. Inline facts below cover the common questions; cross-refs link to deep material.

## Layout

```
tfy-custom-guardrails/
├── README.md                                    Repo overview, quick start
├── CLAUDE.md                                    (this file)
├── docs/                                        Cross-integration docs
│   ├── gateway-contract.md                      THE TFY custom-guardrail HTTP contract
│   ├── integrations-overview.md                 Concept doc: what custom guardrails are, how they fit
│   ├── guardrail-test-phrases.md                Vendor-agnostic demo / test prompt catalog
│   └── add-a-new-integration.md                 Contributor onboarding flow
├── .claude/skills/truefoundry-custom-guardrail/ The reusable skill (SKILL.md + 4 references)
└── integrations/
    ├── _template/                               Skeleton for new integrations
    ├── nemo/                                    NVIDIA NeMo Guardrails (example: LLM-judged)
    └── guardrails-ai/                           Guardrails AI Hub validators (example: heuristic)
```

## The custom-guardrail HTTP contract (MEMORIZE)

**Single source of truth**: [`docs/gateway-contract.md`](docs/gateway-contract.md). Summary inline:

| Wrapper response | Gateway interpretation |
|---|---|
| `HTTP 200` + `{"verdict": true}` | Allow — rail did not fire |
| `HTTP 200` + `{"verdict": false, "message": "<reason>"}` | **Block** — gateway propagates as `guardrail_checks_failed` |
| `HTTP 200` + `{"verdict": true, "transformed": true, "result": {<body>}}` | Mutate (only if dashboard `Operation: Mutate`) |
| Any non-2xx (5xx) | Real error. Routed through dashboard's `Fail on error` policy. |

**HTTP status carries only "completed vs errored." Verdict lives in the JSON body.** Never return 4xx for a policy decision.

**`Fail on error: false`** is the correct default on the current gateway (rail-block and outage are distinguishable). Set to `true` only for safety-critical rails where transient outages should fail-closed.

**Pre-`a1c551be` history** (May 2026): older gateways treated any non-2xx as failure (no distinction between deliberate block and transient error). Wrappers using `HTTP 400` blocks must be migrated to `2xx + verdict: false` against current gateways. See `gateway-contract.md` "Pre-`a1c551be` history".

**Selector format in `X-TFY-GUARDRAILS` header**: `<group-name>/<config-name>`. Example:

```python
extra_headers={"X-TFY-GUARDRAILS": json.dumps({
    "llm_input_guardrails":  ["<group>/<input-config>"],
    "llm_output_guardrails": ["<group>/<output-config>"],
})}
```

## Canonical wrapper architecture

For a new custom guardrail, copy `integrations/_template/` and edit. The shape:

```
<vendor>/
├── main.py                       FastAPI app, routes via app.add_api_route(), bearer auth, /debug
├── entities.py                   Pydantic models (PER INTEGRATION — not shared)
├── guardrail/
│   ├── _<vendor>_runner.py       Optional: shared module-import singleton for heavy init
│   ├── _helpers.py               Optional: last_user_text, first_assistant_text
│   ├── <rail>_input.py           One file per input rail
│   └── <rail>_output.py          One file per output rail
├── config/                       Optional: vendor config files (DSL files, prompts, etc.)
├── setup.py                      Optional: build-time vendor installs (private package registries, etc.)
├── tests/test_smoke.py           pytest with FastAPI TestClient, module-scoped fixture
├── deploy.py                     TFY Python SDK manifest
├── Dockerfile, requirements*.txt, .env.example, README.md, docs/
```

**Files live at the integration root, NOT under `app/`.** Old `app/main.py` pattern is deprecated.

## TFY SDK deploy gotchas (the six footguns)

Documented in [`.claude/skills/truefoundry-custom-guardrail/references/deployment-playbook.md`](.claude/skills/truefoundry-custom-guardrail/references/deployment-playbook.md). Quick recap:

1. **`load_dotenv(override=True)`** — without it, stale shell vars silently win over `.env`.
2. **Pop `TFY_API_KEY`, `WRAPPER_API_KEY`, and any vendor token env vars from `os.environ`** before SDK init. TFY SDK reserves `TFY_API_KEY` for its own auth.
3. **`Service.image` needs `Build(build_spec=DockerFileBuild(...))`**, not bare `DockerFileBuild`.
4. **`Port(host=...)`** validates against cluster-configured hosts. Shared base domains need a `path`; path regex requires leading AND trailing `/`.
5. **Image build cache can serve stale layers.** After every redeploy: `curl /debug/loaded-config` to verify.
6. **Three-way bearer auth sync**: TFY secret → pod env → dashboard Custom Bearer Auth field. All three must match exactly.

## Reference integrations — vendor-specific quick facts

### NeMo Guardrails (`integrations/nemo/`)

LLM-judged input + output rails. Catches **jailbreaks, role-play attacks, system-prompt extraction, policy evasion** via NeMo's `self_check_input` / `self_check_output` flows. Judge LLM call routed back through the TFY gateway for unified audit trail.

- **Endpoints**: `/health`, `/debug/loaded-config`, `POST /self-check-input`, `POST /self-check-output`
- **Tests**: 9/9 pytest (live judge LLM tests skip without `TFY_API_KEY` + `TFY_BASE_URL`)
- **Deploy**: `cd integrations/nemo && .venv/bin/python deploy.py --wait`
- **Repo docs**: `integrations/nemo/{README.md, docs/DESIGN.md, docs/blog-*.md, docs/public-docs-*.md}`

Vendor gotchas:
- **NeMo (v0.21.0)**: env vars don't expand in YAML. `model: ${JUDGE_MODEL}` is literal — wrapper's `guardrail/_nemo_runner.py:_materialize_config` copies config to tempdir and runs `os.path.expandvars()`.
- **`is_content_safe` parser is inverted**: yes=block, no=allow. Phrase prompts "Should this be blocked? yes/no" not "Is this safe? yes/no".
- **Output rails want a user message in history.** Wrapper forwards the last user message; empty string if none.
- **Module-import singleton**: `_nemo_runner.py` instantiates `RailsRunner()` at import time. NeMo init (~1-2s) once, all per-rail handlers share.
- **Latency**: ~1.2-1.5s per direction (one judge LLM call).

### Guardrails AI (`integrations/guardrails-ai/`)

7 per-rail endpoints wrapping 4 Guardrails Hub validators. Catches **structured PII (email/SSN/phone/credit-card), code-style secrets (API keys/JWTs), toxic language, profanity**. No LLM judge — all local heuristics/small classifiers, sub-100ms steady-state per rail.

- **Endpoints**: `/health`, `/debug/loaded-config`, plus 7 rails:
  `POST /{detect-pii,secrets-present,toxic-language}-{input,output}` + `POST /profanity-free-output`
- **Tests**: 13/13 pytest (skip if hub validators not installed locally)
- **Deploy**: `cd integrations/guardrails-ai && .venv/bin/python deploy.py --wait`
- **Repo docs**: `integrations/guardrails-ai/{README.md, docs/DESIGN.md, docs/blog-*.md, docs/public-docs-*.md}`

Vendor gotchas:
- **Guardrails AI (v0.9.3, pinned from GitHub)**: PyPI package quarantined. Pin to GitHub tag in `requirements.txt`: `guardrails-ai @ git+https://github.com/guardrails-ai/guardrails.git@v0.9.3`.
- **`Guard.use()` takes validator INSTANCES, not class+kwargs.** Right: `Guard().use(DetectPII(on_fail="exception"))`. Wrong: `Guard().use(DetectPII, on_fail="exception")`.
- **Chained `.use().use().use()` REPLACES** (only last validator runs). Spread `.use(a, b, c)` is flaky in v0.9.3. **One Guard per validator** — chain in Python code, not at the Guard level.
- **`DetectPII` default `pii_entities='pii'` is too aggressive.** Flags benign prompts. Use a tight allowlist — see `integrations/guardrails-ai/guardrail/_pii_entities.py`.
- **Hub validators install at Docker build time** via `setup.py` (build arg for the Hub token). Token not present at runtime.
- **Context-sensitive accuracy gaps**: Presidio's SSN needs context-word boosting; detect-secrets is tuned for code, not prose. See `integrations/guardrails-ai/docs/DESIGN.md` "Known accuracy gaps".

## Reusable skill

`.claude/skills/truefoundry-custom-guardrail/` is the **full playbook** for adding a new custom-guardrail integration end-to-end:

- `SKILL.md` — 7-phase workflow (validate vendor → build wrapper → tests → deploy → register in gateway → verify → document)
- `references/wrapper-architecture.md` — canonical file layout + code templates
- `references/gateway-contract.md` — verbatim HTTP contract with `guard.ts` code excerpts
- `references/deployment-playbook.md` — full `deploy.py` template + the six footguns
- `references/gotchas.md` — distilled lessons across contract/deployment/vendor/debugging

Packaged zip at `.claude/skills/truefoundry-custom-guardrail.skill` for claude.ai/customize/skills upload.

## Open / unresolved

Don't accidentally redo these:

- **Mutation mode** (PII redaction-in-place, output rewriting) is a v2 candidate — not built in either reference integration.
- **Per-tenant config via `req.config.config_id`** is a v2 candidate — not built.
- **Tenant gateway version dependency**: the 2xx+verdict contract requires `tfy-llm-gateway` commit `a1c551be` or later. Smoke-test on a new tenant before relying on the new shape; older gateways need `Fail on error: true` and the wrapper must use the legacy 4xx-block path.
