# Design notes

How and why the wrapper is shaped the way it is. Read [`README.md`](../README.md) first for the quickstart; this doc is for "I'm about to change something non-trivial and need the context."

## Problem

The TrueFoundry AI Gateway lets customers plug in custom guardrails over a simple HTTP contract. The current contract (post `tfy-llm-gateway` commit `a1c551be`, May 2026) is:

```
POST <user-server>/<endpoint>
{ requestBody, [responseBody], context, config }
  -> 200 {"verdict": true}                     => pass through
  -> 200 {"verdict": false, "message": "..."}  => block (return error to caller)
  -> 200 {transformed: true, result: {...}}    => mutate (only with Operation=Mutate)
  -> 5xx error                                 => guardrail failure (per failOnError policy)
```

We want to use **NVIDIA NeMo Guardrails** as the engine behind this contract. NeMo doesn't natively fit:

- NeMo is *itself* an LLM-wrapping toolkit. Its primary surface is `POST /v1/chat/completions` — give it a user message plus a `guardrails.config_id`, it runs input rails → calls an LLM → runs output rails → returns a guarded `ChatCompletion`. There is **no built-in "check this content, return verdict" endpoint**.
- When a rail blocks, NeMo communicates that by **replacing the assistant message content with a refusal** (e.g. `"I'm sorry, I can't respond to that."`). The detail lives in `result.log.activated_rails` only when you ask for it.
- Configuration is via **Colang** (NVIDIA's DSL) + YAML in a config directory.

So we adapt: we don't use NeMo as an LLM — we use it as a rail runner.

## Why a thin Python wrapper (vs. alternatives)

Three integration paths were considered. See the team's "Gateway Integrations - Support SOP":

| Path | What | Verdict |
|---|---|---|
| **A. Custom guardrail wrapper** (this repo) | Tiny FastAPI service that exposes the TFY contract and delegates to NeMo's Python library in rails-only mode. | **Chosen.** Zero changes to `tfy-llm-gateway`. Ships now. Customer hosts it. Easy to evolve. |
| B. NeMo's own server as a Custom Endpoint | Run `nemoguardrails server` and register its OpenAI-compat endpoint. | Architecturally wrong — NeMo becomes "the model," not a guardrail. Loses gateway-side cost tracking and prevents other guardrails from running on the route. |
| C. Native plugin in `tfy-llm-gateway` | Add `src/plugins/nvidia-nemo-guardrails/` + sf-server CUE schema, mirroring `grayswan-cygnal` / `google-model-armor`. | Significant engineering across two repos. Worth doing later if demand is sustained; gated on sf-server access. |

The SOP itself recommends starting with the custom path while a native path is in flight, and to use the custom path "while we are still validating product-market fit for the integration."

## Architecture

```
   ┌─────────────────────────────────────────────────┐
   │           TrueFoundry AI Gateway                │
   │   (input hook → LLM → output hook)              │
   └─────────────────────────────────────────────────┘
         │                                ▲   │
         │ POST /self-check-input         │   │ POST /self-check-output
         │ {requestBody}                  │   │ {requestBody, responseBody}
         ▼                                │   ▼
   ┌─────────────────────────────────────────────────┐
   │   nemo-guardrails-tfy (this service)            │
   │   FastAPI (root-level main.py + entities.py)    │
   │     /health  /debug/loaded-config               │
   │     /self-check-input   /self-check-output      │
   │       │                          ▲              │
   │       │ ValidateGuardrailResponse(verdict, msg) │
   │       │                          │              │
   │       ▼                          │              │
   │   guardrail/<rail>.py            │              │
   │     │ calls runner.check_input/output           │
   │     ▼                                           │
   │   guardrail/_nemo_runner.py (module singleton) │
   │     │ generate_async(messages, options=         │
   │     │   GenerationRailsOptions(input | output)) │
   │     ▼                                           │
   │   NeMo Guardrails (Python library)              │
   │     ├─ config.yml      (rails registry)         │
   │     ├─ prompts.yml     (judge prompts)          │
   │     └─ self_check_*    (built-in flows)         │
   └─────────────────────────────────────────────────┘
         │
         │ judge call (OpenAI-compat)
         ▼
   ┌─────────────────────────────────────────────────┐
   │  TrueFoundry AI Gateway (again)                 │
   │    JUDGE_MODEL via /v1/chat/completions         │
   └─────────────────────────────────────────────────┘
```

**Per-rail endpoints, not composite.** Each NeMo rail (`self_check_input`, `self_check_output`) gets its own POST endpoint mapped to its own file under `guardrail/`. The dashboard registers them as separate Custom Guardrail Configs. This matches the canonical `truefoundry/custom-guardrails-template` repo conventions adopted across the team.

Why send the judge call back through the TFY gateway rather than directly to an LLM provider:

- One audit trail. Every token spent — by the customer's main request *and* by the guardrail — shows up in the same dashboard.
- One key-management story. The wrapper holds a single TFY API key; routing/quotas/cost-limits stay in the gateway.
- Trivial model swaps. To switch judges from gpt-4o to claude-haiku, change `JUDGE_MODEL`; no code change.

## File layout

```
nemo-guardrails-tfy/
├── main.py                     FastAPI app: routes, bearer auth, /debug
├── entities.py                 Pydantic models for the gateway contract
│                               (ValidateGuardrailResponse, MutateGuardrailResponse,
│                                InputGuardrailRequest, OutputGuardrailRequest)
├── guardrail/
│   ├── _nemo_runner.py         Shared RailsRunner singleton (module-import init)
│   ├── self_check_input.py     Input rail handler
│   └── self_check_output.py    Output rail handler
├── config/
│   ├── config.yml              NeMo passthrough config + model env-var refs
│   └── prompts.yml             self_check_{input,output} prompts (few-shot)
├── tests/test_smoke.py         pytest, 9 cases
├── deploy.py                   TFY Python SDK deployment manifest
├── Dockerfile
└── docs/                       (this directory)
```

The `_nemo_runner.py` is a module-import singleton: `runner = RailsRunner()` at module scope. NeMo's `RailsConfig.from_path` + `LLMRails` construction is expensive (~1-2s); doing it once at import time amortizes the cost. Both `self_check_input.py` and `self_check_output.py` `from guardrail._nemo_runner import runner` and call `runner.check_input(...)` / `runner.check_output(...)`.

## Request flow

`POST /self-check-input`:

1. FastAPI's bearer-auth dependency validates `Authorization: Bearer $WRAPPER_API_KEY`. Mismatch → 401.
2. `self_check_input(req)` pulls the last `user` message out of `req.requestBody.messages` via `_last_user_message`. Vision-style list-of-parts content is flattened; image parts are ignored.
3. Empty / no user message → short-circuit `ValidateGuardrailResponse(verdict=True)` (FastAPI serializes as `200 + JSON`).
4. Otherwise: `await runner.check_input(user_msg)`.
5. RailsRunner calls `LLMRails.generate_async(messages=[{role:user,content:...}], options=GenerationOptions(rails={"input": True, "dialog": False, "output": False, "retrieval": False}, log={"activated_rails": True}))`. NeMo's `self_check_input` flow asks the judge LLM "should this be blocked?" and parses with `is_content_safe`.
6. Verdict translation (see "Verdict mapping" below) produces a `RailVerdict(decision, refusal, activated)`.
7. Handler returns `ValidateGuardrailResponse(verdict=True)` on allow, or `ValidateGuardrailResponse(verdict=False, message=refusal)` on block. **Always HTTP 200.**

`POST /self-check-output` mirrors this — pulls `responseBody.choices[0].message.content`, threads the last user message in as conversational context for NeMo, runs output-rail-only generation, translates the same way.

## Verdict mapping (NeMo → TFY contract)

NeMo's `generate_async` returns a `GenerationResponse` with `.response` (messages after rails ran) and `.log.activated_rails` (which rails fired and their `decisions`).

Internal `RailVerdict` produced by `_nemo_runner.py:_verdict`:

| Signal | Decision |
|---|---|
| `activated_rails` empty | `allow` (no rail fired) |
| any rail decision in `{stop, refuse, abort, block}` | `block` (refusal text from `.response[-1].content`) |
| `activated_rails` non-empty AND content changed but no `stop` token | `block` (defensive — NeMo replaced content even without explicit stop verb) |
| `activated_rails` non-empty AND content unchanged | `allow` (rail fired for logging only) |

The set of "stop"-style decision tokens is defensive (`BLOCK_DECISIONS` in `_nemo_runner.py`). NeMo's internal token has been `stop` historically; the others are belt-and-braces for future NeMo versions.

Handler then maps `RailVerdict` to the wire response:

| Verdict | HTTP response |
|---|---|
| `allow` | `200 + {"verdict": true, "message": null}` |
| `block` | `200 + {"verdict": false, "message": "<refusal text>"}` |

`mutate` is not produced today — `self_check_*` flows block-or-allow. To enable mutation, switch validators to a fix-on-fail variant or write a Colang flow that returns a rewritten body, and add a `MutateGuardrailResponse` branch to the handler.

## Why rails-only mode

NeMo's default behavior is to also generate an answer after input rails pass. We don't want that — the customer's TFY gateway will generate the real answer with the real model. So we:

- Set `passthrough: True` in `config.yml`. With passthrough on, when input rails pass NeMo *doesn't* call the main LLM for content generation.
- Disable dialog/output/retrieval in the per-request `GenerationRailsOptions` when we only want to run input rails (mirror for output).

The judge LLM is still called by `self_check_input` (that's the rail's job). What we suppress is the *main* LLM call to "answer the question," which the customer's actual model will do later.

## v1 rail bundle

- **Input**: `self_check_input` — single LLM-judged decision: should we block this user message?
- **Output**: `self_check_output` — same, applied to the assistant response.

Both use prompts in `config/prompts.yml` with NeMo's built-in `is_content_safe` parser. The parser's convention is **yes = block, no = allow** — easy to get wrong, see "Gotchas" below.

Why this minimal bundle:

- Universal. No model-specific dependencies; works against any TFY-routable model.
- Cheap. One LLM call per direction, on the chosen judge model. ~1.2-1.5s per direction in production against `openai-main/gpt-4o`.
- Composable. Edit `prompts.yml` to tighten/loosen the policy, or add more rails by listing them in `config.yml` and dropping `.co` files under `config/rails/`.

What we deliberately skipped for v1 (and where to start if you add them):

| Rail | Why skipped | How to add |
|---|---|---|
| `self_check_facts` | Needs retrieved context; only relevant to RAG flows. | Add `self check facts` to `rails.output.flows`, supply context via `relevant_chunks`. |
| `self_check_hallucination` | Requires multiple LLM samples → 3-4× the latency. | Add the rail and accept the cost. |
| `topical_rails` | Heavy Colang authoring; opinionated. | Write a custom `.co` flow under `config/rails/`. |
| Llama Guard / NIM `content_safety` | Requires deploying NVIDIA's content-safety NIM. | Add a `content_safety` model entry in `config.yml` and register the rail. |

## Gotchas (real bugs we hit)

### 1. NeMo doesn't expand `${ENV_VAR}` in its YAML

NeMo's YAML loader takes the literal string. When we wrote `model: ${JUDGE_MODEL}`, the openai client tried to POST to a host literally named `${TFY_BASE_URL}` and got a connection error.

**Fix** (in `guardrail/_nemo_runner.py:_materialize_config`): before handing the config dir to `RailsConfig.from_path`, copy it into a tempdir while running `os.path.expandvars()` on every `.yml`/`.yaml` file. Non-YAML files (`.co`, etc.) are copied unchanged.

Side effect: edits to `config/*.yml` require restarting the process (the tempdir is built once at import time). For dev, `uvicorn --reload` watches `.py` files, not YAML — `touch main.py` to force a rebuild.

### 2. `is_content_safe` parser is inverted from what you'd think

Documented in NeMo's `output_parsers.py`:

> "yes" implies the content should be blocked, is unsafe, or violates a policy.
> "no" implies the content is safe or does not violate any policy.

This is the **opposite** of how a human would write a "is this allowed?" prompt. If your prompt asks "Is this message allowed? yes/no", the LLM will answer "yes" for safe content, and NeMo will read that as BLOCK. Every verdict will be inverted.

**Fix**: write the prompt with the convention `yes = block, no = allow`. Our `config/prompts.yml` does this explicitly: *"Should the above user message be blocked? Answer with exactly one word: yes (block) or no (allow)."*

If you ever switch to a custom parser, update the prompt to match.

### 3. Output rails need a (faked) user message

NeMo's output rails assume a conversational history — they want a `user` message before the `assistant` message they're checking. We forward `requestBody.messages` last user message as that user turn. If the request only had `system` messages, we pass an empty string; rails still run (they're checking the assistant content), but if you write a rail that depends on `user_input`, it will see `""`.

### 4. Verifying which build is actually live

`/debug/loaded-config` returns a `wrapper_version` field driven by the `BUILD_REF` env var, which `deploy.py` sets to the git short SHA (suffixed `-dirty` for an unclean working tree, epoch-fallback if git is unavailable). After a redeploy:

```bash
curl -sS -H "Authorization: Bearer $WRAPPER_API_KEY" \
  https://<host>/<path>/debug/loaded-config | jq .wrapper_version
```

A stale value means either the cluster is still on an older image or git wasn't reachable at deploy time — either way, dig before concluding "new code is live." The same field shows up alongside the prompt SHA-256 digests, so you can cross-reference what's running vs. what's checked in.

## Gateway contract — pre vs post commit `a1c551be`

The wrapper originally returned `HTTP 400 + {error, message, activated_rails}` for blocks. That was a workaround for the pre-May-2026 gateway, which treated any 2xx as "passed" and any non-2xx (4xx and 5xx alike) as "failed" with no way to distinguish deliberate blocks from transient errors. The only setting that made blocks actually block was `Fail on error: true`, which conflated rail decisions with real outages.

`tfy-llm-gateway` commit `a1c551be` (PR #2931) extends `src/plugins/custom/guard.ts` to read an explicit `verdict` field from the wrapper's 2xx body. With this, rail decisions live in the JSON body and the HTTP status code only carries "completed (2xx) vs errored (5xx)." `Fail on error: false` is the correct default — real outages and rail decisions are now distinguishable.

The wrapper now returns:

- `200 + {"verdict": true}` for pass
- `200 + {"verdict": false, "message": "..."}` for block
- 5xx for real failures (judge LLM unreachable, NeMo crash, etc.) — gateway applies `failOnError` policy

Customers on older gateway versions: keep `Fail on error: true` on the guardrail configs. The wrapper's 200-with-verdict-false will register as a generic 2xx-pass on the old gateway (no block!) — verify with a smoke test before relying on the new shape on an unverified tenant.

## Failure modes

| Failure | Where | Surface |
|---|---|---|
| Judge LLM call fails (network, 5xx) | NeMo's `langchain` adapter → bubbles up | Wrapper returns **500** to the gateway. Gateway applies `failOnError` policy. |
| Wrong / missing bearer | `require_bearer` | 401 with `detail`. |
| Empty `messages` (input) or empty `choices` (output) | `_last_user_message` / `_first_assistant_message` short-circuit | 200 + `{"verdict": true}`. No NeMo call. |
| NeMo `RailsConfig.from_path` blows up at import | module-level `runner = RailsRunner()` in `_nemo_runner.py` | Pod fails to start. Health check never goes green. Look at startup logs. |
| Build failed on TFY but `deploy.py --wait` exited 0 | `service.deploy(wait=True)` returning before TFY transitions to `BUILD_FAILED` | The post-deploy `activeVersion == lastVersion` assert in `deploy.py` catches this and exits 1. Without it, the old image keeps serving silently. See `references/deployment-playbook.md` footgun #6. |

## What's out of scope (today)

- **Streaming.** The TFY custom-guardrail contract is buffered: the gateway calls `/self-check-output` after the LLM response is complete. Streaming guardrails would require a different contract.
- **Per-tenant rail configs.** The wrapper loads one `config/` at import time. To support multiple configs, switch to looking up by `req.config.get("config_id")` and pre-loading several `LLMRails` instances. NeMo supports it; we just don't expose it yet.
- **Latency budget.** ~1.2-1.5s per call against `openai-main/gpt-4o`. Could be cut with a smaller judge (e.g. Haiku, gpt-4o-mini) or by running input + output rails as TFY `Validate` (parallel).
- **Caching.** No verdict cache. Same exact input → same LLM call every time. Worth adding if costs/latency become a concern; cache key = hash(prompt + judge model + rail set).
- **Mutation.** Both rails run block-or-allow. To enable PII-style mutation, see the per-rail handler comments.

## Future work

In rough order of payoff:

1. Add structured logging (per-request `subjectId` from `context.user`) and Prometheus-style metrics: rail verdict counter, judge latency histogram, judge error counter.
2. Support multiple named rail bundles selectable per request via `config.config_id`. Useful for tenant-specific or team-specific policy variants.
3. Add a content-safety NIM rail (Llama Guard via NVIDIA NGC) as an alternative to LLM-judged self-checks for high-volume routes where latency matters.
4. Verdict cache for high-volume identical-prompt scenarios (e.g. health-check probes, deduplicated agent traffic).
5. If demand justifies it, promote to a native plugin in `tfy-llm-gateway` (SOP §5). Reuse this wrapper's verdict-mapping logic verbatim in the plugin handler.
