# RepelloAI Argus — TrueFoundry AI Gateway custom guardrail

Guardrail wrapper that scans gateway traffic with [RepelloAI Argus](https://repello.ai). Four rails, one Argus asset, all policy configuration owned by the Argus dashboard.

| Rail | Dashboard Operation | Scans | Redacts |
|---|---|---|---|
| `POST /validate-input` | Validate | latest user message, new tool results | — |
| `POST /validate-output` | Validate | every assistant choice, every tool call's arguments | — |
| `POST /redact-input` | Mutate | latest user message, new tool results | not yet — see [Redaction](#redaction) |
| `POST /redact-output` | Mutate | every assistant choice, every tool call's arguments | yes |

Validate rails block or allow. Redact rails apply Argus's masked text in place and block whatever cannot be safely redacted.

**Register one operation per hook.** Either `validate-output` or `redact-output`, not both: `result` replaces the whole response body, so two rails on one hook each return a full body and the second overwrites the first. Quota doubles too. Redact when the masked text should reach the caller; validate when the request should hard-stop.

## Quickstart

```bash
cd integrations/repelloai-argus
cp .env.example .env      # fill in ARGUS_API_KEY, ARGUS_ASSET_ID, WRAPPER_API_KEY
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q
.venv/bin/uvicorn main:app --port 8000
```

The wrapper **refuses to start** unless `ARGUS_API_KEY`, `ARGUS_ASSET_ID` and `WRAPPER_API_KEY` are all set, and it verifies the asset ID against Argus before serving traffic. Both checks are deliberate: see [Misconfiguration is invisible at runtime](#misconfiguration-is-invisible-at-runtime).

Deploy to TrueFoundry:

```bash
pip install -U truefoundry && tfy login
python deploy.py --wait
```

Then in **AI Gateway → Guardrails → Add New Guardrails Group**, register each rail you want as its own Custom Guardrail Config, with the `Operation` from the table above and **Custom Bearer Auth** set to your `WRAPPER_API_KEY`. Callers select them as `<group-name>/<config-name>` in the `X-TFY-GUARDRAILS` header.

Operation must match the rail. A redact rail registered under `Operation: Validate` still returns 200, but the gateway ignores the mutate fields and the redaction is silently discarded.

Recommended error handling: fail closed on the input rail, fail open on the output rail. Depending on gateway version this is a `Fail on error` toggle or an enforcing strategy such as `enforce_but_ignore_on_error`. It only affects genuine wrapper failures; a policy block is a 200 with `verdict: false` and always blocks.

## Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ARGUS_API_KEY` | yes | — | Argus runtime SDK key (`rsk_...`) |
| `ARGUS_ASSET_ID` | yes | — | Default asset whose dashboard policies apply |
| `WRAPPER_API_KEY` | yes | — | Bearer token the gateway presents to this wrapper |
| `ARGUS_API_BASE` | no | `https://argusapi.repello.ai/sdk/v1` | Argus SDK base URL |
| `ARGUS_TIMEOUT_S` | no | `6.0` | Per-call timeout; must stay below the gateway's 10 s |
| `ARGUS_MAX_TEXT_CHARS` | no | `20000` | Max characters sent per content unit |

The dashboard **Config** field is optional and overrides the environment for that guardrail config only:

```json
{
  "assetId": "asset-for-this-application",
  "credentials": {"apiKey": "rsk_..."}
}
```

Either key may be given on its own; anything absent falls back to the environment. This lets a multi-tenant gateway point different applications at different Argus assets, or different Argus accounts, without deploying a copy of the wrapper per tenant.

The three environment variables remain required regardless: the wrapper verifies `ARGUS_ASSET_ID` with `ARGUS_API_KEY` at startup, before any Config is in play. A key supplied only through Config is never validated at startup and will surface as a 503 on first use if wrong.

> Config is stored gateway-side and is readable by anyone with dashboard access to that guardrail config. Prefer the deploy secret for the API key, and use the Config override only where per-tenant credentials are actually needed.

## What gets scanned

Argus has no conversational model: every call scans one standalone piece of text with no history. This wrapper matches that exactly. It splits each gateway payload into independent **content units** and scans each on its own, concurrently.

| Content unit | Source | Argus endpoint |
|---|---|---|
| Latest user message | `requestBody.messages`, last `role: user` | `/analyze/prompt` |
| Tool results since that message | `requestBody.messages`, `role: tool` | `/analyze/prompt` |
| Each assistant text choice | `responseBody.choices[].message.content` | `/analyze/response` |
| Each tool call's arguments | `responseBody.choices[].message.tool_calls[]` | `/analyze/response` |

Only content **new in this request** is scanned; earlier turns were already scanned when they passed through the gateway.

Two of these are easy to miss. Tool results are the standard prompt-injection carrier: a poisoned RAG chunk or MCP response never appears in a user message. And a tool-calling response has `content: null`, so a wrapper that only reads `content` skips the output rail on exactly the responses that can exfiltrate data through tool arguments.

## Per-rail policy coverage

Argus applies different checks to prompts and responses. Enabling "the Argus input guardrail" does **not** give you PII coverage.

These are the exact names Argus emits, which are the names that appear in block messages. The suffix is not uniform: `unsafe_prompt_protection`, everything else `_detection`.

| Argus policy | `/analyze/prompt` | `/analyze/response` | Needs config? |
|---|---|---|---|
| `prompt_injection_detection` | yes | no | no |
| `unsafe_prompt_protection` | yes | no | no |
| `banned_topics_detection` | yes | no | **yes** — topic list |
| `unsafe_response_detection` | no | yes | no |
| `pii_detection` | no | yes | no — redacts on the redact rails |
| `secrets_keys_detection` | no | yes | **yes** — custom regex patterns |
| `system_prompt_leak_detection` | no | yes | **yes** — the system prompt to match against |
| `toxicity_detection` | yes | yes | no |
| `competitor_mention_detection` | yes | yes | **yes** — competitor names |
| `policy_violation_detection` | yes | yes | **yes** — policy text |


**Five policies do nothing until you give them metadata**, and they report no error in that state. With `secrets_keys_detection` enabled but unconfigured, a real-format AWS access key and a GitHub token pass cleanly; supplying one regex makes the same input block. `system_prompt_leak_detection` and `banned_topics_detection` behave the same way. Enabling the guardrail does not by itself give you credential blocking. Policy configuration lives entirely in Argus, so a TrueFoundry operator cannot see this state from this side; `/debug/loaded-config` is the workaround.

## Capacity

Argus rate-limits **500 requests per 60 seconds per API key**, shared across both scan endpoints. Independent per-unit scanning trades throughput for coverage, so do this arithmetic before load-testing:

| Traffic shape | Argus calls per gateway request | Gateway ceiling |
|---|---|---|
| Plain chat (both rails) | 2 | ~4.1 req/s |
| Agentic turn, 3 tool results + 2 tool calls | 7 | ~1.2 req/s |

Past the ceiling Argus returns 429, the wrapper returns 503, and the gateway's `Fail on error` decides. With the default `false` that means **traffic passes unguarded precisely when load is highest**, which is why `Fail on error: true` is recommended on the input rail.

Quota is consumed per content unit, and `save` is always on so events appear in the Argus dashboard.

## Failure behavior

| Condition | Validate rails | Redact rails |
|---|---|---|
| All units pass | `200 {"verdict": true}` | `200 {"verdict": true, "transformed": false}` + original body |
| Any unit flagged, none blocked | `200 {"verdict": true}` + log | mask applied, `transformed: true` |
| Any unit blocked | `200 {"verdict": false, "message": "Blocked by RepelloAI Argus (...): <policy names>"}` | `200 {"verdict": false, "transformed": false}` + original body |
| Flagged but unmaskable | n/a | `200 {"verdict": false, "transformed": false}` + WARNING log |
| Flagged but longer than `ARGUS_MAX_TEXT_CHARS` | n/a | `200 {"verdict": false, "transformed": false}` + WARNING log |
| No scannable content | `200 {"verdict": true}`, no Argus call | `200 {"verdict": true, "transformed": false}` + WARNING log |
| Argus 429 (rate limit or quota) | `503 {"error": "argus_rate_limited"}` | same |
| Timeout / unreachable / 5xx / bad JSON / missing verdict | `503` with a distinct `error` | same |
| Unrecognized verdict string | `503 {"error": "unrecognized_verdict"}` — never treated as a pass | same |

Policy decisions are always HTTP 200. Non-2xx is reserved for real failures, per the gateway contract. Block messages contain policy names only, and `MutateGuardrailResponse` carries no message field at all — the gateway's mutate branch reads only `verdict` and `transformed` — so redact-rail denials are diagnosed from the WARNING logs.

Argus's `details.text` field returns the detected secret or PII verbatim and `masked_result` echoes the scanned content, so neither is ever logged or surfaced to callers.

A zero-unit result on a redact rail logs at WARNING rather than INFO: `verdict: true, transformed: false` is otherwise indistinguishable from "scanned, nothing to redact", when in fact nothing was scanned. It is legitimate for a tool-call-only response or empty `choices`, but it is also what a body this wrapper cannot parse produces: a streaming chunk, or a non-OpenAI-shaped payload, is forwarded unscanned. Watch this log line if a provider's response shape is unclear.

## Misconfiguration is invisible at runtime

If an Argus asset has no active policies, Argus skips evaluation and returns a completely normal `passed` verdict. Nothing in the response distinguishes "scanned and clean" from "no policies are running", so a misconfigured guardrail looks perfectly healthy while passing 100% of traffic.

Argus's API has no endpoint listing an asset's configured policies. (`policies_applied` is declared on the response schema, but `/analyze/*` never returns it.) Three things compensate:

1. The wrapper verifies `ARGUS_ASSET_ID` at startup and refuses to boot if Argus rejects it. A bad ID also returns a hard 404 per request, which becomes a 503.
2. `/debug/loaded-config` reports `scan_stats.observed_policies`, the policy names seen in violations since startup. Many scans with an empty list suggests an asset with no active policies, policies enabled but unconfigured, or everything set to flag.
3. `Fail on error: true` on the input rail converts Argus outages into blocks rather than silent passes.

None of these catch a policy that is enabled, set to block, and missing its metadata. Five of the ten policies need metadata, and that state is invisible from this side; check it in the Argus dashboard.

## Known limitations

- **Multi-turn attacks are not detected.** Argus scans each unit independently with no history, so a jailbreak assembled across several turns is invisible. This is an Argus capability limit, not a wrapper choice.
- **Several policies are silent no-ops without dashboard metadata.** See the coverage table above.
- **No streaming support.** Argus scans complete text.
- **`redact-input` does not redact yet**, and multimodal content that needs a mask is blocked rather than redacted. See [Redaction](#redaction).

## Redaction

The redact rails apply Argus's `masked_result`: a full replacement for exactly the text that was submitted. Because the wrapper scans one content unit per call, each unit's masked string goes straight back to the field it came from. Argus returns one replacement string per unit, which the wrapper applies as-is — it never merges strings or reconstructs offsets.

Three rules decide what the caller receives:

1. **A mask is never permission to allow.** The verdict comes from the violated policies' actions, not from the presence of a masked string: a block can come from a non-redacting policy while a redacting one did the masking.
2. **Blocked content is never forwarded redacted.** Any blocking policy denies and returns the original body.
3. **Flagged content that could not be redacted is never forwarded.** A redacting policy that fired with no mask means masking was attempted and failed, which is indistinguishable from having nothing to mask. The rail denies and logs at WARNING.

Two shapes cannot be rewritten in place and therefore deny when they need a mask, each logging at WARNING:

- **Multimodal (list-shaped) content.** Scanning joins the text parts with newlines, which is not invertible, and writing the joined mask back as a bare string would delete image parts. Content needing no mask passes through untouched.
- **Nothing else** — tool-call arguments are handled: a masked string is written back as a string, and arguments that arrived as a decoded dict are restored as a dict whenever the masked text still parses. If masking breaks the JSON the masked string is written anyway, since redacted-but-restringified beats forwarding the unredacted value.

`redact-input` does not redact yet. Neither redacting policy runs on the prompt endpoint, so it enforces blocks and passes content through unchanged. It is written against the same contract as the output rail, so the day a redacting policy is enabled for prompts it begins redacting with no change here.

Redaction costs the same quota as validation — one Argus call per content unit. Mutate rails need the full body, so they require buffered (non-streaming) responses; the gateway buffers before calling output rails.

Which entities get redacted is decided by Argus policy configuration, not by this wrapper, which applies whatever `masked_result` comes back. Coverage can change in Argus without any change here, so check the policy configuration rather than this document to know what a given asset masks.

## Development

```bash
.venv/bin/python -m pytest tests/ -q     # 49 tests, no secrets or network required
```

Argus is mocked with `respx`. See [docs/DESIGN.md](docs/DESIGN.md) for architecture and the reasoning behind each decision.
