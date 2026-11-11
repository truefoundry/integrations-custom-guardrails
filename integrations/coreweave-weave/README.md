# coreweave-weave-guardrails-tfy

CoreWeave Weave scorers behind the TrueFoundry custom-guardrail HTTP contract. v1 wraps the **WeaveToxicityScorerV1** (Celadon, a DeBERTa-v3-small with 5 toxicity heads trained on Toxic Commons) as input and output rails.

The scorer runs locally in the pod -- no calls to W&B at runtime. Per-call latency is ~25-30 ms on CPU after warmup. The Celadon model (~550 MB) is baked into the Docker image at build time so cold-start avoids the ~6 min HF download stall.

## Endpoints

```
GET  /health                          health check
GET  /debug/loaded-config             bearer-auth gated; lists scorer + thresholds + routes

# Validate (block on toxicity)
POST /toxicity-input                  score the last user message; block on detect
POST /toxicity-output                 score the first assistant response; block on detect

# Mutate (mask on toxicity; same scorer, different response shape)
POST /toxicity-input-mutate           score the last user message; replace its content on detect
POST /toxicity-output-mutate          score the first assistant response; replace its content on detect
```

Auth on all POSTs: `Authorization: Bearer $WRAPPER_API_KEY`.

## Response contract

Per [`../../docs/gateway-contract.md`](../../docs/gateway-contract.md). The validate and mutate rails return different shapes:

**Validate rails** (`/toxicity-input`, `/toxicity-output`):

| Status | Body | Meaning |
|---|---|---|
| `200` | `{"verdict": true}` | Pass |
| `200` | `{"verdict": false, "message": "..."}` | Block; gateway returns `guardrail_checks_failed` to caller |
| `5xx` | error JSON | Real error; dashboard's `Fail on error` policy applies |

Block message format: `WeaveToxicity ({input,output}): blocked on <category> (score=N, total=M, thresholds={...})`. Category is the highest-scoring of `Race/Origin`, `Gender/Sex`, `Religion`, `Ability`, `Violence`.

**Mutate rails** (`/toxicity-input-mutate`, `/toxicity-output-mutate`):

| Status | Body | Meaning |
|---|---|---|
| `200` | `{"verdict": true, "transformed": false, "result": <original body>}` | Pass through unchanged (no toxicity detected) |
| `200` | `{"verdict": true, "transformed": true, "result": <modified body>}` | Mutate; gateway uses `result` to replace the in-flight body |
| `5xx` | error JSON | Real error |

On mutation, the input rail replaces the last user message's `content` with `"[message removed by safety filter]"`. The output rail replaces the first assistant choice's `content` with `"I can't help with that."` Celadon is a scorer (not a rewriter) so the replacement is a fixed string rather than a sanitized rewrite of the original — for semantically-preserving rewrites see the `integrations/lasso-security/` `classifix` rails.

## Per-request configuration

The dashboard's **Config** JSON field is passed through to the rail and can override scorer thresholds without redeploying:

```json
{
  "total_threshold": 5,
  "category_threshold": 3
}
```

Wrapper defaults: `total_threshold=5`, `category_threshold=3`. The category default is tuned **one step above** Weave's own default (`2`) because Celadon scores short capitalized greetings like `"Hi"` and `"Hey"` at Race/Origin=2 — false positives in playground use. Score 3+ is where the classifier reliably indicates real hate / death threats / overt slurs. Lower `category_threshold` to 2 if your traffic is more formal-prose-like and you want to catch milder harassment ("you are a worthless idiot" scores 2). Lower to 1 to catch veiled threats too (see "Known accuracy gaps").

## Known accuracy gaps

Captured during Phase 1 vendor validation and a post-deploy playground session against Celadon (see `weave_scorers_smoke.ipynb` in the `pg-integrations` working dir):

- **Short capitalized greetings false-positive at score=2.** "Hi" and "Hey" both score Race/Origin=2; "Hello", "Hi there", and lowercase "hi" all score 0. This is what motivated the wrapper's `category_threshold=3` default — score=2 is a noisy band that mixes real "mild harassment" signals with these false positives.
- **Mild harassment scores 2.** Phrases like "you are a worthless idiot", "you are a fucking idiot and I hope you die slowly", and "I will find you and hurt you" all score Violence=2. They will **pass** the default `category_threshold=3`. Real hate / death threats / overt slurs ("Go kill yourself", "I hate Indians", "All women are stupid") score 3+ and block. If your policy needs the milder harassment to block too, set `{"category_threshold": 2}` per-rail in the dashboard Config and accept the greeting false positives.
- **Veiled threats** like "I hope someone breaks her face" score Violence=1, below both the wrapper default and Weave's own default. Set `{"category_threshold": 1}` to catch them at the cost of more noise.
- Celadon does **not** detect prompt injection, secret leakage, or PII. It is a toxicity classifier only. For PII use the `guardrails-ai` integration in this repo.
- Label-space quirk: homophobic content tends to score Race/Origin=3 rather than Gender/Sex=3. The 5 dimensions are conceptual, not orthogonal — the block message names the top-scoring dimension which is informative but not always semantically tidy.

## Local run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env  # fill in WRAPPER_API_KEY
.venv/bin/uvicorn main:app --reload --port 8000
```

First run downloads Celadon (~550 MB, one-time, ~6 min on a home connection).

## Tests

```bash
.venv/bin/pytest -v tests/
```

Live-scorer tests auto-skip if `weave[scorers]` isn't importable; the plumbing tests (health, bearer auth, short-circuit cases) always run.

## Deploy

The wrapper is a standard Docker container. Host it anywhere Docker runs and is reachable from your TFY Gateway (ECS, Cloud Run, Kubernetes, on-prem, or as a TrueFoundry Service via the included `deploy.py`).

**Example: deploy as a TrueFoundry Service**

```bash
.venv/bin/pip install -U truefoundry
tfy login
.venv/bin/python deploy.py --wait
```

**Example: deploy anywhere else** — `docker build -t coreweave-weave-tfy .`, run with `WRAPPER_API_KEY` set, route a public HTTPS URL to port 8000.

The Dockerfile pre-downloads Celadon during image build so the deployed pod starts warm. Image size: ~3 GB (Python base + torch CPU + transformers + Celadon).

## Dashboard registration

After deploy, register the rails as Custom Guardrail Configs under one **Group** (`coreweave-weave`). Validate and mutate rails are independent — register only the ones you want to apply.

Shared per-config fields: `Auth = Custom Bearer Auth` with the `wrapper-api-key` secret value, `Headers = (empty)`, `Config = {}` (or override thresholds — see "Per-request configuration"), `Fail on error = false` (post `tfy-llm-gateway` commit `a1c551be`).

| Config name | URL | Operation |
|---|---|---|
| `toxicity-input` | `https://<host>/<path>/toxicity-input` | `Validate` |
| `toxicity-output` | `https://<host>/<path>/toxicity-output` | `Validate` |
| `toxicity-input-mutate` | `https://<host>/<path>/toxicity-input-mutate` | `Mutate` |
| `toxicity-output-mutate` | `https://<host>/<path>/toxicity-output-mutate` | `Mutate` |

Per-request selector for the `X-TFY-GUARDRAILS` header — pick validate OR mutate per direction (do not stack both on the same direction):

```json
{
  "llm_input_guardrails":  ["coreweave-weave/toxicity-input"],
  "llm_output_guardrails": ["coreweave-weave/toxicity-output"]
}
```

Or to mask instead of block:

```json
{
  "llm_input_guardrails":  ["coreweave-weave/toxicity-input-mutate"],
  "llm_output_guardrails": ["coreweave-weave/toxicity-output-mutate"]
}
```

## Vendor reference

- WeaveToxicityScorerV1 docs: https://weave-docs.wandb.ai/guides/evaluation/weave_local_scorers/
- HF model: https://huggingface.co/wandb/WeaveToxicityScorerV1 (Apache 2.0; W&B re-host of `PleIAs/celadon`)
- Backbone paper: https://huggingface.co/PleIAs/celadon

## v2 candidates (not in this wrapper)

The same pattern can wrap any of `weave.scorers.Weave*ScorerV1`. Notable additions:

- `WeaveHallucinationScorerV1` (HHEM 2.1, Vectara) -- output rail; needs RAG source context channel.
- `WeaveContextRelevanceScorerV1` (DeBERTa-base-long-nli) -- input rail for RAG.
- `WeaveTrustScorerV1` -- composite over the above.

These are deferred to v2 pending a design conversation on how to plumb RAG source documents through the gateway's `requestBody` / `context` shape.
