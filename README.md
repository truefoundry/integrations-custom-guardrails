# tfy-custom-guardrails

TrueFoundry AI Gateway custom-guardrail integrations. One repo, many self-contained integrations.

Each `integrations/<vendor>/` directory is an independently-deployable FastAPI service that conforms to the [TrueFoundry custom-guardrail HTTP contract](docs/gateway-contract.md). The TFY gateway calls these services at the `llm_input` and `llm_output` hooks; they return a verdict and the gateway honors it.

> **New integrator? Start here**: [`docs/add-a-new-integration.md`](docs/add-a-new-integration.md)
>
> **Working with Claude Code? Open [`CLAUDE.md`](CLAUDE.md) first.**

## Current integrations

| Integration | Vendor | Endpoints |
|---|---|---|
| [`integrations/nemo/`](integrations/nemo/) | NVIDIA NeMo Guardrails (LLM-judged rails) | `/self-check-input`, `/self-check-output` |
| [`integrations/guardrails-ai/`](integrations/guardrails-ai/) | Guardrails AI Hub validators (local heuristics) | `/detect-pii-{input,output}`, `/secrets-present-{input,output}`, `/toxic-language-{input,output}`, `/profanity-free-output` |

Each integration's deployed URL is configured in its own `deploy.py` (`TFY_PUBLIC_HOST` + `TFY_PUBLIC_PATH`) and varies by tenant. The `deploy.py` prints the resolved URLs after a successful run.

Add a new integration: see [`docs/add-a-new-integration.md`](docs/add-a-new-integration.md).

## Repo layout

```
tfy-custom-guardrails/
├── README.md                       This file
├── CLAUDE.md                       Context for Claude Code sessions
├── docs/                           Cross-integration documentation
│   ├── gateway-contract.md         THE TFY custom-guardrail HTTP contract (single source of truth)
│   ├── integrations-overview.md    What custom guardrails are, why this repo exists, how integrations fit together
│   ├── guardrail-test-phrases.md   Vendor-agnostic test prompt catalog
│   └── add-a-new-integration.md    Step-by-step contributor flow
├── .claude/skills/                 Reusable skill for new integrations
│   └── truefoundry-custom-guardrail/
└── integrations/
    ├── _template/                  Skeleton — `cp -r _template/ <new-vendor>/`
    ├── nemo/                       NVIDIA NeMo Guardrails wrapper (example)
    └── guardrails-ai/              Guardrails AI Hub validators wrapper (example)
```

## Design principle

**Each integration is fully self-contained.** No shared `entities.py`, no shared auth helpers, no shared deploy.py. The gateway contract is small enough that contract drift between integrations is handled at code-review time, not by enforced sharing. Read the rationale in [`docs/gateway-contract.md`](docs/gateway-contract.md) "Why no shared code".

Trade-offs we accept:

| Decision | Trade-off |
|---|---|
| Per-integration `entities.py` | When the gateway contract changes, update each integration's copy. Coordinated PR. |
| Per-integration `main.py`, `deploy.py`, `Dockerfile` | Each integration deploys independently with full freedom to pin different dependencies. |
| Per-integration venv | Each integration has its own `.venv/`. Vendor deps cannot conflict across integrations. |
| Cross-integration docs at top level | Single place for the contract, the demo prompts, the contributor flow, the skill. |

## Quick start

Clone, pick an integration to work on, work inside its directory:

```bash
git clone <repo-url>
cd tfy-custom-guardrails/integrations/<vendor>
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env  # fill in the values
.venv/bin/uvicorn main:app --reload --port 8000
```

To add a new vendor:

```bash
cp -r integrations/_template integrations/<new-vendor>
# Edit integrations/<new-vendor>/{main.py, guardrail/, requirements.txt, deploy.py, README.md, ...}
# Run tests, deploy, register in TFY dashboard.
# Full step-by-step: docs/add-a-new-integration.md
```

## Where the wider context lives

- **What custom guardrails are + how to think about them**: [`docs/integrations-overview.md`](docs/integrations-overview.md).
- **The HTTP contract**: [`docs/gateway-contract.md`](docs/gateway-contract.md). Single source of truth.
- **Vendor-agnostic demo prompts** (allow controls + violations across PII / secrets / toxicity / jailbreak / etc.): [`docs/guardrail-test-phrases.md`](docs/guardrail-test-phrases.md).
- **Reusable skill** that future Claude Code sessions or contributors can use to scaffold a new integration: [`.claude/skills/truefoundry-custom-guardrail/`](.claude/skills/truefoundry-custom-guardrail/).
- **Team SOP** (defines the §1–§5 integration paths — Custom Endpoint, Custom Guardrail, OTEL Exporter, Native Provider, Native Guardrail): held by the gateway team, not in this repo. Ask the team if you need a copy.
