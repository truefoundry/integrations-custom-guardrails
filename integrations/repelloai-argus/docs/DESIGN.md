# Design notes

How and why the RepelloAI Argus wrapper is shaped the way it is. Read [`../README.md`](../README.md) for the quickstart; this doc is for "I'm about to change something non-trivial and need the context."

## Problem

The TrueFoundry AI Gateway lets customers plug in custom guardrails over a simple HTTP contract (post `tfy-llm-gateway` commit `a1c551be`, May 2026):

```
POST <user-server>/<endpoint>
{ requestBody, [responseBody], context, config }
  -> 200 {"verdict": true}                     => pass through
  -> 200 {"verdict": false, "message": "..."}  => block
  -> 200 {verdict: true, transformed: true, result: {...}}  => mutate (Operation=Mutate only)
  -> 5xx error                                 => guardrail failure (per failOnError)
```

We want to use **[RepelloAI Argus](https://repello.ai)** behind that contract. Argus is a policy engine: a customer configures policies on an **asset** in the Argus dashboard, and each call to `/analyze/prompt` or `/analyze/response` scans one standalone piece of text against that asset's policies and returns a verdict of `passed` / `flagged` / `blocked`. Argus has no conversational model, and its only mutation output is an optional full-replacement `masked_result` string.

The wrapper translates between the gateway's OpenAI-shaped bodies and Argus's scan API, splitting each request into independently-scannable content units and mapping Argus verdicts back to `ValidateGuardrailResponse`.

## Why a thin Python wrapper (vs. alternatives)

Three integration paths were considered:

| Path | What | Verdict |
|---|---|---|
| **A. Custom guardrail wrapper** (this repo) | FastAPI service that POSTs to Argus's `/analyze/prompt` and `/analyze/response` per rail. | **Chosen.** Zero changes to `tfy-llm-gateway`. Ships now. |
| B. Argus as a Custom Endpoint | Register Argus's own server URL as the model endpoint. | Wrong shape — Argus is not the LLM; guardrails must run at the gateway hooks. |
| C. Native plugin in `tfy-llm-gateway` | Add `src/plugins/repelloai-argus/`. | Cross-repo work; defer until demand justifies a native gateway plugin. |

Argus is SaaS-only with a structured HTTP API — a strong fit for the custom-guardrail path while validating product-market fit.

## Architecture

```
                    ┌───────────────────── TrueFoundry AI Gateway ─────────────────────┐
 caller ──X-TFY-GUARDRAILS──▶ llm_input hook                    llm_output hook        │
                    └──────────┼──────────────────────────────────────┼────────────────┘
                               │ /validate-input                      │ /validate-output
                               │ /redact-input                        │ /redact-output
                               ▼                                      ▼
        ┌──────────────────── repelloai-argus wrapper (FastAPI, async) ──────────────────┐
        │  bearer check (compare_digest) ──fail──▶ 401                                   │
        │  asset_id = config.assetId or ARGUS_ASSET_ID                                   │
        │                                                                                │
        │  split payload into INDEPENDENT content units (no history, no concatenation)   │
        │      input : latest user msg | each new tool-result msg                        │
        │      output: each choice's text | each tool_call's arguments                   │
        │                    │                                                           │
        │                    ▼   asyncio.gather over one shared httpx.AsyncClient        │
        │            ┌───────┴───────┬───────────────┬───────────────┐                   │
        │         scan u1         scan u2         scan u3        scan uN    (timeout 6s) │
        │            └───────┬───────┴───────────────┴───────────────┘                   │
        │                    ▼                                                           │
        │  aggregate: any blocked → block | any flagged → allow+log | else allow          │
        │                    ▼                                                           │
        │  validate: 200 {"verdict": bool, "message"?}                                    │
        │  redact  : 200 {"verdict": bool, "transformed": bool, "result": {full body}}    │
        │            └─ masks written back per unit; block or unmaskable → original body  │
        │  either  : 503/500 {"error","detail"} on any upstream failure                   │
        └────────────────────────────────────────────────────────────────────────────────┘
                               │  X-API-Key, asset_id, save=true, user_id, session_id
                               ▼
        ┌──── Argus ── /analyze/prompt   → input-applicable policies  ──────────────────┐
        │              /analyze/response → output-applicable policies                   │
        └───────────────────────────────────────────────────────────────────────────────┘
```

**Four endpoints: validate and redact, per direction.** Each rail gets its own POST route and its own Custom Guardrail Config in the dashboard. The redact rails reuse the validate rails' scan and differ only in what they do with the result — see "Redaction applies one string per unit" below.

**SaaS round-trip per content unit.** Every content unit fans out its own call to `argusapi.repello.ai`. Budget latency and quota accordingly (default timeout 6s, well inside the gateway's 10s budget); see Capacity in the README.

## Verdict mapping

```
per content unit:
  Argus call
   ├── 429 (rate limit OR quota exhausted) ─────▶ ArgusRateLimited     ▶ 503
   ├── timeout / connection / non-2xx / bad JSON ▶ ArgusApiError        ▶ 503
   ├── verdict field missing ────────────────────▶ ArgusApiError        ▶ 503
   └── verdict present
         ├── not in {passed, flagged, blocked} ──▶ unrecognized_verdict ▶ 503
         ├── "blocked"  ──▶ unit blocks (verdict is the authority)
         ├── "flagged"  ──▶ unit allowed, recorded
         └── "passed"   ──▶ unit allowed
              (also what a zero-policy asset returns — indistinguishable here,
               which is why the asset is verified at startup instead)

aggregate over all units (validate rails):
   any exception          ──▶ 503 / 500          [gateway's Fail on error decides]
   any unit blocking      ──▶ 200 {"verdict": false, "message": "<policy names>"}
   any unit flagged       ──▶ 200 {"verdict": true} + log
   all passed             ──▶ 200 {"verdict": true}
   zero inspectable units ──▶ 200 {"verdict": true}, no Argus call, warning logged

aggregate over all units (redact rails) — `result` is always a full body:
   any exception            ──▶ 503 / 500        [identical to the validate rails]
   any unit blocking        ──▶ verdict=false, transformed=false, original body
   redacting policy fired,
     masked_result is null  ──▶ verdict=false, transformed=false, original body + warning
   unit is list-shaped and
     needs a mask           ──▶ verdict=false, transformed=false, original body + warning
   any unit has a mask      ──▶ mask written back at the unit's index
   zero inspectable units   ──▶ verdict=true, transformed=false, original body + warning
   final transformed        ──▶ recomputed by comparing the bodies, not trusted from Argus
```

Block messages look like: `Blocked by RepelloAI Argus (input): {policy names}`. The redact rails carry no message — `MutateGuardrailResponse` has no such field and the gateway's mutate branch reads only `verdict` and `transformed` — so their denials are diagnosed from the WARNING logs.

## Configuration surface

| Source | Keys | Purpose |
|---|---|---|
| Deploy env / TFY secret | `ARGUS_API_KEY` | Argus runtime SDK key, sent as `X-API-Key` |
| Deploy env | `ARGUS_API_BASE` | Override API base (default `https://argusapi.repello.ai/sdk/v1`) |
| Deploy env | `ARGUS_ASSET_ID` | Default asset whose dashboard policies apply |
| Dashboard Config JSON | `assetId`, `credentials.apiKey` | Per-config overrides; each falls back to the env var when absent |
| Deploy env | `ARGUS_TIMEOUT_S`, `ARGUS_MAX_TEXT_CHARS` | Per-call timeout and truncation length |
| Gateway context | `context.user.subjectSlug` / `subjectId` | Maps to Argus `user_id` |
| Gateway context | `context.metadata.request_id` / `session_id` / `sessionId` | Maps to Argus `session_id` |

Wrapper bearer auth (`WRAPPER_API_KEY`) is independent of the Argus API key.

## Repo layout

```
repelloai-argus-guardrails-tfy/
├── main.py                 FastAPI app: routes, bearer auth, /debug/loaded-config
├── entities.py              Pydantic models (validate + mutate responses, request types)
├── guardrail/
│   ├── __init__.py
│   ├── argus.py             Argus HTTP client, verdict mapping, validate rails
│   ├── redact.py            Mutate rails: mask write-back and the safety rules
│   └── _helpers.py           Content-unit extraction (input_units / output_units)
├── deploy.py                TFY Python SDK deployment manifest
├── Dockerfile
├── requirements.txt
├── .env.example
├── tests/test_smoke.py
└── docs/
    ├── DESIGN.md            (this file)
    └── public-docs-repelloai-argus.md
```

Unlike NeMo and Guardrails AI, the rail handlers are grouped by operation rather than one file per rail: all four share the same HTTP client, scanning loop, and content-unit extraction. `argus.py` owns the scan and the validate rails; `redact.py` adds only the mask write-back and the safety rules on top of the same `scan_request`.

## Decisions

**Redaction applies one string per unit and never merges.** Argus returns `masked_result` as a full replacement for exactly the text submitted in `scan_data` — one string per scan, with any overlapping matches already resolved. Combined with per-unit scanning, where each unit's mask replaces the field it came from, the wrapper performs no offset arithmetic and never combines two masked strings. That property is what makes redaction safe here: there is no partial-merge case in which a secret could survive while the response reports `transformed: true`.

**Redaction is a separate pair of rails, not a mode.** `redact-*` are distinct routes rather than a flag on the validate rails, because the dashboard `Operation` is per-config and the two shapes are not interchangeable: a mutate-shaped response under `Operation: Validate` is silently discarded. Separate routes also let an operator run validation on one hook and redaction on the other.

**A unit longer than the scan cap is never masked.** Argus masks exactly the text it was sent, and that text is truncated to `ARGUS_MAX_TEXT_CHARS`. Writing the mask back over the full field would delete the unscanned tail, so an oversized unit denies with the original body instead. This is what keeps the one-string-per-unit property above true: the wrapper never writes a mask that covers less than the field it replaces.

**Blocked or unmaskable content is never forwarded.** Masking and verdict are computed independently upstream, so a non-null `masked_result` can accompany a blocked verdict. The redact rails decide from the violated policies' actions and discard the mask on any block. When a redacting policy fires but no mask comes back, masking was attempted and failed — indistinguishable from having nothing to mask — so the rail denies rather than forwarding the original.

**A redacting policy is detected by the per-policy `masked_result` key, not by name.** Argus attaches that key only to violated policies that mask. `policy_name` on the wire is the customer's dashboard label rather than a stable key, so matching on names would break on a rename and would need updating whenever a redacting policy is added.

**Independent per-unit scanning, not a concatenated transcript.** Argus has no conversational model, so each call scans one standalone piece of text. Concatenating the conversation would also bury the content that matters: an injection commonly arrives in a tool result, and an exfiltration attempt in a tool call's arguments on a response whose `content` is `null`. A wrapper reading only `messages[-1].content` and `choices[0].message.content` misses both. Scanning each unit separately keeps every one of them in scope. The cost is call volume; see Capacity in the README.

**The verdict is the blocking authority, not parsed policy names.** Deriving "blocked" from `policies_violated` means an unparseable violations array turns a block into an allow. `UnitResult.is_blocking` returns true when the verdict is `blocked` *or* any policy carries `action_taken == "block"`, so neither a malformed body nor a verdict/action disagreement can silently pass traffic.

**`action_taken == "block"` is matched exactly.** The field echoes whatever action the policy is configured with, and the non-blocking values are not a fixed set (both `flag` and `flagged` occur). Anything that is not an explicit block is treated as advisory.

**Verdicts are `passed` / `flagged` / `blocked` strings, and anything else is an error.** A zero-policy asset returns a normal `passed`, which is why that state is undetectable from the response and has to be inferred from `observed_policies` instead. Unrecognized verdict strings raise rather than falling through to allow.

**`save` is hardcoded true.** This affects correctness, not just dashboard visibility: the asset ID is only validated when `save` is true, so `save=false` lets a typo'd asset pass all traffic. Quota consumption is the same either way.

**Async client with concurrent fan-out.** A blocking client under FastAPI would serialise N units onto the threadpool; at several seconds per Argus call an agentic request would exceed the gateway's 10s budget. One `httpx.AsyncClient` lives in the lifespan, giving connection reuse instead of a TLS handshake per unit.

**Timeout 6s, not 10s.** The gateway's budget is 10s, so a 10s client timeout would never fire first and the caller would get an opaque gateway error instead of a 503 naming the cause. Some policies are slower than others and can exceed 6s; timing out is the intended visible failure.

**Startup validation over lazy failure.** Missing env vars and an unverifiable asset ID both abort startup. A wrapper that boots without credentials passes its health check and then 5xxs every request, which under the default `Fail on error: false` means unguarded traffic. Validation runs in the lifespan rather than at import, so the modules stay importable without secrets and CI can collect tests.

**Defer to the platform's fail-open default.** TrueFoundry's `Fail on error` defaults to fail-open, and this integration does not try to override it. The deploy output and README both recommend `Fail on error: true` for the input rail, where failing open carries the most risk.

**Dashboard Config overrides the environment, which stays the default.** A single global asset and key would force every application behind a multi-tenant gateway through one policy set and one Argus account. `config.assetId` and `config.credentials.apiKey` each take precedence when present; deployments that set neither keep the single-asset behaviour.

The env vars stay mandatory even so, because startup validation needs a credential and an asset before any request arrives. That is the tradeoff: a per-config key is never verified at boot and can only fail at request time, so the deploy secret remains the recommended source and Config the exception. Config is also stored gateway-side and readable by anyone with dashboard access to that config, which is a wider audience than the deploy secret.

## Gateway behaviors that affect this integration

From TrueFoundry's [custom guardrails docs](https://www.truefoundry.com/docs/ai-gateway/custom-guardrails):

**Input `Validate` rails may run alongside the in-flight model request.** The gateway is documented to run input validation concurrently with the model call "when applicable", whereas `Mutate` rails run sequentially. A block is still enforced, but the upstream model call may already have been issued — so the input rail should be understood as preventing the *response reaching the caller*, not reliably preventing model spend. Output and MCP hooks always run synchronously before the content is released.

**`Operation` must match the rail.** The validate rails return a bare verdict and need `Operation: Validate`; the redact rails return `transformed` plus a full OpenAI-shaped `result` and need `Operation: Mutate`. The mismatch is silent in one direction: a redact rail registered as `Validate` still returns 200 while the gateway ignores the mutate fields and discards the redaction.

**Register one operation per hook.** `result` replaces the whole body, so two rails on the same hook each return a full body with no defined merge and the second overwrites the first. Nothing in the contract passes state between rails, so a second rail cannot be assumed to see the first's output. Quota doubles as well.

**`config` is per-config, not per-request.** It comes from the dashboard field and can be changed without redeploying, which is what makes the `assetId` override useful for multi-tenancy.

**MCP tool guardrails are a separate hook.** The gateway can run guardrails on MCP tool calls, where `context.metadata.claims` carries verified JWT claims from the caller. This wrapper does not implement that hook — it scans tool calls and tool results as they appear in the LLM request/response bodies, which is a different interception point. Wiring Argus into the MCP hook is a plausible v2.

## Security notes

`details.text` in an Argus violation contains the **detected secret or PII in plaintext**. Consequently:

- Argus response bodies are never logged at any level, including debug. Error logs carry the status code and an error identifier only.
- Block messages contain policy names only, never detected values.
- `/debug/loaded-config` is bearer-gated and reports secret **presence and length**, never values.

There is a test asserting that neither the scanned content nor a detected secret appears in captured logs.

## Diagnostics

Argus's SDK API exposes no endpoint listing an asset's configured policies. `policies_applied` is declared on the response schema but `/analyze/*` never returns it. So "the switch is on but nothing is running" cannot be read directly.

`/debug/loaded-config` returns `scan_stats`: total scans, a breakdown by verdict, error and rate-limit counters, and `observed_policies` — the policy names seen in violations since startup. A long run of scans with an empty `observed_policies` is the signature of an asset with no active policies, policies enabled but unconfigured, or everything set to flag.

Neither layer detects the subtler case: a policy enabled with an action of block but with empty metadata, which five of the ten policies require. That is invisible from this side by construction, and is called out in both the README and the setup guide instead.

## API behavior this wrapper depends on

Argus is treated as a scan endpoint that takes text and returns a verdict. The observable contract the wrapper is built against:

- Verdicts are the strings `passed` / `flagged` / `blocked`. Anything else is treated as an error, never as an allow.
- Violations carry `policy_name` and `action_taken`; `action_taken` echoes the action the policy is configured with. Only `policy_name` is read.
- `details.text` returns the detected secret or PII verbatim, so violation bodies are never logged or forwarded.
- A response carries no indication of which policies ran, so an asset with nothing enabled is indistinguishable from a clean scan. Hence the startup asset check and `observed_policies`.
- `masked_result` is a full replacement for exactly the text submitted, not edit spans. It appears both at the top level and, on violated policies that mask, per policy. It is null when nothing matched *and* when masking failed, which is why a fired-but-unmasked redacting policy denies.
- An invalid asset ID under `save=true` returns 404, which the wrapper surfaces as a 503.
- Typical scan latency sits well inside the 6s timeout.

## Failure modes

| Failure | Where | Surface |
|---|---|---|
| Argus 429 (rate limit or quota exhausted) | `_scan_unit` | Wrapper returns **503** with `error: argus_rate_limited`. |
| Timeout / unreachable / 5xx / bad JSON / missing verdict | `_scan_unit` | Wrapper returns **503** with a distinct `error` per cause. |
| Unrecognized verdict string | `_scan_unit` | Wrapper returns **503** with `error: unrecognized_verdict` — never treated as a pass. |
| Wrong / missing bearer | `require_bearer` | 401 with `detail`. |
| Missing required env var at startup | `_require_env` in `main.py` lifespan | Pod refuses to start; crash-looping surfaces the misconfiguration instead of serving unguarded traffic. |
| Argus rejects the configured asset ID at startup | `verify_asset` in lifespan | Pod refuses to start, rather than passing all traffic silently at request time. |
| No scannable content in the payload | short-circuit in every rail | 200 + allow, no Argus call. On the redact rails this logs at WARNING, since `transformed: false` is otherwise indistinguishable from "scanned, nothing to redact". |
| Masking failed after a redacting policy fired | `_redact` rule 3 | Denies with the original body + WARNING. Never forwards content that could not be redacted. |
| Multimodal content needs a mask | `_apply_to_message` | Denies with the original body + WARNING. The scan joins text parts with newlines, which is not invertible. |
| A mask breaks a tool call's JSON | `_apply_to_tool_call` | Masked string written anyway; redacted-but-restringified beats forwarding the unredacted value. |
| Redact rail registered under `Operation: Validate` | Dashboard | Silent: 200 is returned and the gateway discards the redaction. `/debug/loaded-config` reports each route's intended operation. |
| Stale code after redeploy | TFY image cache | `curl /debug/loaded-config` and check `wrapper_version` |

## Future work

1. Per-tenant asset selection beyond the single `config.assetId` override, if multi-asset routing needs grow.
2. Multimodal redaction, contingent on scanning each content part separately. That would break the one-utterance-one-scan property and multiply quota, so the rails deny instead while multimodal traffic is rare.
3. Leaf-level masking of tool-call arguments, so a mask can never land on a JSON key. It needs one Argus call per leaf; the parse-and-restore guard covers the corruption case in the meantime.
4. Promote to a native plugin in `tfy-llm-gateway` if Argus becomes a strategic integration. The verdict-mapping logic in `guardrail/argus.py` ports over directly.
