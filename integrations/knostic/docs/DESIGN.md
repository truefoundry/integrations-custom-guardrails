# Design notes — Knostic custom guardrail wrapper

How and why this integration is shaped the way it is. Read [`../README.md`](../README.md) for the quickstart.

## Problem

The TrueFoundry AI Gateway custom-guardrail contract (post `tfy-llm-gateway` commit `a1c551be`) expects a small HTTP service that returns `verdict` in JSON on HTTP 200 for policy decisions. [Knostic](https://www.knostic.ai) provides enterprise AI security at the **knowledge layer**: need-to-know access control, prompt-injection defense, and Prompt Gateway DLP (inspect prompts/responses, block or mask sensitive content). Those capabilities are a natural fit for gateway input/output hooks, but Knostic's runtime API is tenant-specific and not published as open documentation.

This wrapper bridges OpenAI-shaped gateway bodies and Knostic's HTTP API with configurable paths and flexible response parsing.

## Why a thin Python wrapper

| Path | Verdict |
|---|---|
| **A. Custom guardrail wrapper** (this repo) | **Chosen.** Ships without `tfy-llm-gateway` changes. |
| B. Native plugin in `tfy-llm-gateway` | Defer until API stability and demand justify cross-repo work. |
| C. Local-only heuristics (e.g. porting openclaw-shield regex) | Does not enforce Knostic need-to-know policies or tenant console config. |

## Architecture

```
   TrueFoundry AI Gateway
         │ POST /knostic-prompt-inspect-input|output
         │ POST /knostic-prompt-sanitize-input|output
         ▼
   knostic-guardrails-tfy (FastAPI)
         │ HTTPS + Bearer (KNOSTIC_API_KEY)
         ▼
   Knostic tenant API (Prompt Gateway)
```

**Four per-rail endpoints** — same pattern as Lasso Security. Users attach validate-only, mutate-only, or both.

**SaaS round-trip** — budget latency per request (default timeout 15s). No local ML models in the wrapper.

## Request flow

### Validate (`inspect`)

1. Short-circuit if no user text (input) or assistant text (output).
2. Extract OpenAI-format `messages`.
3. `POST {KNOSTIC_API_BASE}{KNOSTIC_INSPECT_PATH}` with payload:
   - `messages`, `messageType` (`PROMPT` | `COMPLETION`)
   - `sessionId`, optional `userId`, `policyId`, `model`
4. Map response to block if any of:
   - Top-level `action` / `decision` in block set
   - `allowed: false`
   - Any item in `violations` / `findings` / `issues` with block action
5. Return `ValidateGuardrailResponse(verdict=False, message=...)` or `verdict=True`.

### Mutate (`sanitize`)

1. Same extraction and API call to `KNOSTIC_SANITIZE_PATH`.
2. If block signals present → `verdict=False`, `transformed=False`, original body in `result`.
3. Else apply `messages` / `maskedMessages` / `sanitizedText` from Knostic onto request or response body.
4. Return `MutateGuardrailResponse` with `transformed` flag.

## Configuration

| Source | Keys |
|---|---|
| Env | `KNOSTIC_API_KEY`, `KNOSTIC_API_BASE`, `KNOSTIC_INSPECT_PATH`, `KNOSTIC_SANITIZE_PATH`, `KNOSTIC_POLICY_ID`, `WRAPPER_API_KEY` |
| Dashboard `config` | `credentials.apiKey`, `api_base`, `inspect_path`, `sanitize_path`, `policyId`, `sessionId`, `userId`, `timeout` |

Session continuity: `config.sessionId` or `context.metadata` keys (`session_id`, `sessionId`, `knostic-session-id`).

User identity: `config.userId` or `context.user.subjectSlug` / `subjectId`.

## API contract assumptions

Default paths (`/v1/guardrails/inspect`, `/v1/guardrails/sanitize`) and response fields are **placeholders** based on Knostic product descriptions (Prompt Gateway, prompt injection defense). Your tenant may use different paths or field names — override via env or dashboard config.

When Knostic publishes a stable public OpenAPI spec, tighten `_knostic_client.py` to match it and add contract tests against recorded fixtures.

## Gotchas

1. **Confirm API details with Knostic** before production. Wrong base URL or path returns 502/401 from the wrapper, not a silent allow.
2. **Fail on error: false** on gateway configs (post `a1c551be`) so outages (5xx) differ from policy blocks (200 + `verdict: false`).
3. **Mutate rails** require dashboard `Operation: Mutate`. Inspect rails use `Validate`.
4. **Image cache** — after deploy, hit `/debug/loaded-config` and compare `wrapper_version` / `BUILD_REF`.
5. **Knostic vs Kirin** — Kirin secures IDE coding assistants; this wrapper targets **Prompt Gateway / LLM gateway** inline guardrails, not the Kirin desktop product.

## Failure modes

| Symptom | Likely cause |
|---|---|
| 500 "API key not configured" | Missing `KNOSTIC_API_KEY` on pod or in config |
| 401 from wrapper | Knostic key invalid or wrong auth header scheme |
| 502 connection errors | Wrong `KNOSTIC_API_BASE` or network policy blocking egress |
| Always `verdict: true` | Knostic returns non-block shape; check raw API response in smoke notebook |
| Mutate never transforms | Sanitize endpoint returns allow without `messages`; use inspect+block instead |

## Future work

- Recorded OpenAPI fixtures once Knostic publishes tenant API docs
- Optional composite rail that runs inspect then sanitize in one gateway config (not recommended — prefer parallel configs)
- Per-tenant `config_id` routing when gateway supports it
