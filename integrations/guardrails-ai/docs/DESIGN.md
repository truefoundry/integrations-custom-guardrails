# Design notes

How and why the wrapper is shaped the way it is. Read [`../README.md`](../README.md) for the quickstart; this doc is for "I'm about to change something non-trivial and need the context."

## Problem

The TrueFoundry AI Gateway lets customers plug in custom guardrails over a simple HTTP contract (post `tfy-llm-gateway` commit `a1c551be`, May 2026):

```
POST <user-server>/<endpoint>
{ requestBody, [responseBody], context, config }
  -> 200 {"verdict": true}                     => pass through
  -> 200 {"verdict": false, "message": "..."}  => block
  -> 200 {transformed: true, result: {...}}    => mutate (Operation=Mutate only)
  -> 5xx error                                 => guardrail failure (per failOnError)
```

We want to use **Guardrails AI's Hub validators** behind that contract — PII detection, secrets detection, toxic-language classification, profanity filtering. These are local heuristics or small classifiers; no LLM round-trip per request.

The wrapper shape is the canonical one from the `truefoundry-custom-guardrail` skill: a FastAPI service with **one endpoint per validator per direction**, each translating between Guardrails AI's Python API and the TFY contract. Each endpoint is registered as its own Custom Guardrail Config in the dashboard, so users can pin individual validators per request via the `X-TFY-GUARDRAILS` header.

## Why a thin Python wrapper (vs. alternatives)

Three integration paths were considered:

| Path | What | Verdict |
|---|---|---|
| **A. Custom guardrail wrapper** (this repo) | Tiny FastAPI service that calls `Guard.validate()` for each configured validator. | **Chosen.** Zero changes to `tfy-llm-gateway`. Ships now. |
| B. `guardrails start` as a Custom Endpoint | Run Guardrails AI's built-in Flask server and proxy through it. | Wrong shape — Guardrails AI's server wraps an LLM call; we don't want it to be the model. |
| C. Native plugin in `tfy-llm-gateway` | Add `src/plugins/guardrails-ai/`. | Significant cross-repo work; needs sf-server CUE schema. Defer until demand justifies it. |

## Architecture

```
   ┌──────────────────────────────────────────────────┐
   │           TrueFoundry AI Gateway                 │
   │   (input hook → LLM → output hook)               │
   └──────────────────────────────────────────────────┘
         │                                  ▲   │
         │ POST /<rail>-input               │   │ POST /<rail>-output
         │ {requestBody}                    │   │ {requestBody, responseBody}
         ▼                                  │   ▼
   ┌──────────────────────────────────────────────────┐
   │   guardrails-ai-tfy (this service)               │
   │   FastAPI (root-level main.py + entities.py)     │
   │     /health  /debug/loaded-config                │
   │                                                  │
   │   Input rails (3 endpoints):                     │
   │     POST /detect-pii-input    → DetectPII        │
   │     POST /secrets-present-input → SecretsPresent │
   │     POST /toxic-language-input → ToxicLanguage   │
   │                                                  │
   │   Output rails (4 endpoints):                    │
   │     POST /detect-pii-output    → DetectPII       │
   │     POST /secrets-present-output → SecretsPresent│
   │     POST /toxic-language-output → ToxicLanguage  │
   │     POST /profanity-free-output → ProfanityFree  │
   │                                                  │
   │   Each handler lives in guardrail/<rail>.py      │
   │   and owns one Guard.use(<Validator>) instance.  │
   │   ValidateGuardrailResponse(verdict, msg) back.  │
   └──────────────────────────────────────────────────┘
                          │
                          ▼
   ┌──────────────────────────────────────────────────┐
   │   guardrails-ai (Python library)                 │
   │     ├─ DetectPII       → Presidio analyzer       │
   │     ├─ SecretsPresent  → detect-secrets          │
   │     ├─ ToxicLanguage   → Unitary HF classifier   │
   │     └─ ProfanityFree   → local word list         │
   └──────────────────────────────────────────────────┘
```

**Per-validator endpoints, not composite.** Each validator gets its own POST endpoint (per direction). The dashboard registers them as separate Custom Guardrail Configs, so users can pin individual validators (or any subset) per request rather than running all-or-nothing.

**No LLM round-trip per request.** This is the biggest architectural difference from the NeMo wrapper. All validators in the v1 bundle are local (Presidio, detect-secrets, small classifier, list match). Sub-100 ms per validator steady-state.

## Request flow

`POST /detect-pii-input` (and every other input rail, identically):

1. FastAPI's bearer-auth dependency validates `Authorization: Bearer $WRAPPER_API_KEY`. Mismatch → 401.
2. `detect_pii_input(req)` pulls the last `user` message out of `req.requestBody.messages` via `_helpers.last_user_text`. Vision-style list-of-parts content is flattened; image parts are ignored.
3. Empty / no user message → short-circuit `ValidateGuardrailResponse(verdict=True)`. No validator call.
4. Otherwise: `guard.validate(user_msg)` where `guard` is the module-scope `Guard().use(DetectPII(...))`. The Guard is constructed once at module import time.
5. If `guard.validate` returns silently → allow. Handler returns `ValidateGuardrailResponse(verdict=True)`.
6. If it raises (`on_fail="exception"` mode) → block. Handler returns `ValidateGuardrailResponse(verdict=False, message=f"<Validator> (input): {str(e)[:300]}")`.

Every response is **HTTP 200**. Verdict lives in the JSON body per the gateway contract (see "Gateway contract" below).

`POST /detect-pii-output` (and every other output rail): identical structure but pulls `responseBody.choices[0].message.content` via `_helpers.first_assistant_text`.

## Why one Guard per file (not a composite Guard)

Phase 0 finding worth understanding. Guardrails AI v0.9.3 has two surprises in its `Guard.use()` API:

1. **Chained `.use().use().use()` replaces, not appends.** Only the last validator runs.
2. **Spread `.use(a, b, c)` is flaky.** Validators silently no-op when composed (`SecretsPresent` missed an OpenAI key it caught in isolation). Probably a parallel-execution state issue in `async_validator_service`.

**The architectural answer**: never compose validators inside a single Guard. One `Guard().use(<Validator>)` per per-rail file (`guardrail/detect_pii_input.py`, `guardrail/secrets_present_input.py`, etc.), each exposed at its own endpoint. The dashboard composes by stacking Custom Guardrail Configs — that's where composition belongs.

This also matches how the TFY gateway wants to operate: each rail registered as a separate Custom Guardrail Config means dashboards/clients can pin any subset per request via `X-TFY-GUARDRAILS`. "Compose at the gateway, not in the wrapper" is the durable answer.

If a future Guardrails AI version fixes composition we still wouldn't collapse — the per-endpoint shape is what the gateway expects.

## Verdict mapping (Guardrails AI → TFY contract)

Each rail file owns one Guard with one validator in `on_fail="exception"` mode. Outcomes:

| Validator outcome | Wire response |
|---|---|
| `guard.validate(text)` returns silently | **200** + `{"verdict": true, "message": null}` |
| `guard.validate(text)` raises (any exception, not just `ValidationError`) | **200** + `{"verdict": false, "message": "<Validator> (input/output): <err>"}` |
| No user message (input) or empty `choices` (output) | **200** + `{"verdict": true}` — short-circuit before validator call |

The wrapper catches **all** exceptions (not just `ValidationError`) and surfaces them as `verdict: false`. The trade-off: a corrupted validator file or model load failure becomes a block, not an outage. Failure-closed for safety; the message field carries the validator name + truncated exception for debugging.

We do not produce `mutate` verdicts in v1 because all validators are in exception mode. PII redaction-as-mutation would require switching `DetectPII` to `on_fail="fix"` (returns a redacted string instead of raising) and adding a `MutateGuardrailResponse` branch in the relevant per-rail handler.

## v1 validator bundle

- **Input rails** (3 endpoints): `DetectPII`, `SecretsPresent`, `ToxicLanguage`
- **Output rails** (4 endpoints): same three plus `ProfanityFree`

Per-endpoint steady-state latency (each request hits exactly one validator's endpoint):

- `DetectPII` ~10 ms — Presidio analyzer over the message
- `SecretsPresent` ~2 ms — detect-secrets scan
- `ToxicLanguage` ~30 ms — small HF classifier (preloaded weights)
- `ProfanityFree` ~2 ms — word-list match

Order across rails is a **dashboard concern**, not a wrapper concern — register the configs in the order you want them to run; the gateway evaluates them as a chain and short-circuits on the first block.

### Why this bundle vs. heavier validators

The Hub has 50+ validators including `ProvenanceLLM`, `HallucinationCheck`, `JailbreakDetection`, `RestrictToTopic`, `RegexMatch`. We deliberately ship a small heuristic-only bundle in v1:

- No LLM round-trips → predictable latency, no cost per request.
- Local-only validators → no external dependencies in the request path.
- Heavier validators can be added later by dropping a new `guardrail/<rail>-input.py` (and/or `<rail>-output.py`) file, importing it in `main.py`, and adding the route to `RAIL_ROUTES`. The dashboard configuration is per-rail.

## Build-time validator install (not runtime)

Hub validators are Python packages from `pypi.guardrailsai.com` (a private index). Installing them requires authentication via `guardrails configure --token`. We do this **at Docker build time**, not runtime:

- The `GUARDRAILS_TOKEN` is a build-arg (passed via TFY secret ref in `deploy.py`).
- The Dockerfile runs `guardrails configure` + `guardrails hub install` for each v1 validator.
- The image ships with validators pre-installed; runtime never needs the token.

Trade-offs of this approach:
- **Pros**: token doesn't live in runtime env; cold-start is much faster (no install on boot); validators are pinned to whatever versions were on the Hub at build time.
- **Cons**: validator version changes require a rebuild; you cannot hot-swap validators in the running pod.

To refresh validators or change the bundle: edit `setup.py`'s `guardrails hub install` lines (which run at build time), add/remove per-rail files under `guardrail/`, wire them into `RAIL_ROUTES` in `main.py`, and redeploy.

## Why route nothing back through the TFY gateway

The NeMo wrapper routes its judge LLM calls back through the TFY gateway for unified observability. This wrapper has no LLM calls, so there's nothing to route. `TFY_BASE_URL` and `JUDGE_MODEL` are deliberately absent from `.env.example` and `deploy.py`.

If a future validator (e.g. `HallucinationCheck`, which requires an LLM judge) is added, that LLM call should be routed through the TFY gateway following the same pattern as NeMo. Don't call provider APIs directly.

## Gateway contract — pre vs post commit `a1c551be`

The wrapper originally returned `HTTP 400 + {error, message, activated_rails}` for blocks. That was a workaround for the pre-May-2026 gateway, which treated any 2xx as "passed" and any non-2xx (4xx and 5xx alike) as "failed" with no way to distinguish deliberate blocks from transient errors. The only setting that made blocks actually block was `Fail on error: true`, which conflated rail decisions with real outages.

`tfy-llm-gateway` commit `a1c551be` (PR #2931) extends `src/plugins/custom/guard.ts` to read an explicit `verdict` field from the wrapper's 2xx body. Rail decisions live in the JSON body and the HTTP status code only carries "completed (2xx) vs errored (5xx)." `Fail on error: false` is the correct default — real outages and rail decisions are now distinguishable.

The wrapper now returns:

- `200 + {"verdict": true}` for pass
- `200 + {"verdict": false, "message": "..."}` for block
- 5xx for real failures (validator file corrupted, etc.) — gateway applies `failOnError` policy

Customers on older gateway versions: keep `Fail on error: true` on the guardrail configs. The wrapper's `200 + verdict: false` will register as a generic 2xx-pass on the old gateway (no block!) — verify with a smoke test before relying on the new shape on an unverified tenant.

## Failure modes

| Failure | Where | Surface |
|---|---|---|
| Validator raises non-`ValidationError` exception (model file corrupted, classifier weight load fail) | Each `guardrail/<rail>.py` `try/except Exception` | 200 + `verdict: false` with the validator name + truncated exception in `message`. Failure-closed by design. |
| Wrong / missing bearer | `require_bearer` (in `main.py`) | 401 with `detail`. |
| Empty `messages` (input) or empty `choices` (output) | `_helpers.last_user_text` / `_helpers.first_assistant_text` short-circuit | 200 + `verdict: true`, no validator call. |
| Hub validator not installed at import time | Module-level `Guard().use(<Validator>(...))` in `guardrail/<rail>.py` | Pod refuses to start (ImportError at module load). Look at startup logs; usually means the `setup.py` hub-install step at build time didn't run or `GUARDRAILS_TOKEN` wasn't passed as a build arg. |
| Build failed on TFY but `deploy.py --wait` exited 0 | `service.deploy(wait=True)` returning before TFY transitions to `BUILD_FAILED` | The post-deploy `activeVersion == lastVersion` assert in `deploy.py` catches this and exits 1. Without it, the old image keeps serving silently. See `references/deployment-playbook.md` footgun #6. |

## Known accuracy gaps

Verified during Phase 5 end-to-end testing. The plumbing is correct; these are validator-accuracy limitations:

- **`DetectPII` US_SSN recognizer is context-boosted**: `"My email is X and my SSN is Y"` blocks correctly. `"My SSN is Y, please help me with my taxes"` and bare `"123-45-6789"` do not. Presidio's recognizer requires strong contextual signals; weak context drops below the default score threshold.
- **`SecretsPresent` (detect-secrets) is tuned for code, not prose**: `"Here is my key: sk-proj-..."` blocks. `"Here is my API key: sk-proj-... -- can you echo it?"` and bare `"sk-proj-..."` do not. The detect-secrets engine's own docs warn "best with multiline code snippets."
- **`ToxicLanguage` threshold matters**: at 0.5 we catch obvious insults but miss subtle hostility. Higher precision means more false negatives; lower means more false positives on assertive-but-civil prose.

### Mitigation strategies (v2 candidates)

1. **Lower Presidio's score threshold** (`DetectPII(..., score_threshold=0.3)`). More recalls, more false positives on benign text.
2. **Add a regex-based fallback** for SSN / credit card / well-known API key patterns. Brute force but reliable.
3. **Defense in depth**: position this guardrail as one layer of a multi-layer strategy. Stack with TFY's built-in PII guardrail and application-layer checks. The single-validator approach was never going to catch every adversarial framing; the architectural answer is layered defenses, not chasing perfect recall in one validator.

For v1 the team chose option 3 — document the gap, ship the integration, and tune in v2 based on real-traffic miss reports.

## What's out of scope (today)

- **Streaming-aware rails.** The TFY custom-guardrail contract is buffered.
- **Mutation mode.** All v1 validators run in exception mode. Adding mutate requires per-validator `on_fail` configuration + a mutation branch in the wrapper.
- **Per-tenant validator configs via `req.config`.** The wrapper loads one bundle at startup; the dashboard `Config` field is ignored.
- **LLM-judge validators** (`HallucinationCheck`, `ProvenanceLLM`, etc.). Would re-introduce LLM round-trips and the unified-observability question.

## Future work

In rough order of payoff:

1. Lower DetectPII / SecretsPresent thresholds, evaluate false-positive rate on real traffic, decide on a tuning vs. defense-in-depth trade-off.
2. Add mutation mode for PII redaction (substitute placeholders, return `200 + transformed: true` + mutated body). Customer-ask candidate.
3. Track Guardrails AI's PyPI quarantine status; switch back to plain `pip install guardrails-ai` when restored.
4. Eliminate the `GUARDRAILS_TOKEN` exposure in build logs by switching from Docker `ARG` to BuildKit `--mount=type=secret`. Docker buildkit itself warns about this (`SecretsUsedInArgOrEnv`).
5. Slim the image — current ~6.8 GB image has hit ACR push timeouts once. Multi-stage build dropping `apt`/`pip`/`uv` caches + lazy-loading HF model weights instead of baking them in.
6. Promote to a native plugin in `tfy-llm-gateway` if demand justifies. Validator translation logic stays the same; lives in `src/plugins/guardrails-ai/` instead of in this HTTP wrapper.
