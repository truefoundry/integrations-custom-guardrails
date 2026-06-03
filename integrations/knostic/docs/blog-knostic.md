# Knostic need-to-know guardrails on the TrueFoundry AI Gateway

Enterprise teams adopt LLMs through gateways so they can centralize auth, routing, and policy. Knostic secures the layer those gateways do not see by default: what knowledge an answer may draw from, whether a prompt is trying to hijack the model, and whether a completion leaks data the user should not receive.

This post describes how we wired Knostic's Prompt Gateway into TrueFoundry as a **custom guardrail** — a small FastAPI service the gateway calls at `llm_input` and `llm_output` hooks.

## Why a wrapper instead of a native plugin

TrueFoundry's custom-guardrail contract is deliberately small: POST a JSON body, get back `verdict: true|false` on HTTP 200. That is enough to integrate any vendor without shipping their SDK inside the gateway.

Knostic's value is in tenant-specific need-to-know graphs and policies configured in their platform. A wrapper keeps that boundary clean: the gateway sees only verdicts; Knostic sees only the messages and user context you forward.

## Architecture

```
Client → TrueFoundry Gateway → knostic-guardrails-tfy → Knostic tenant API
                ↑                        │
                └──── verdict JSON ──────┘
```

We expose **four rails**:

1. **Inspect input** — validate prompts before inference
2. **Inspect output** — validate completions after inference
3. **Sanitize input** — mutate (mask) prompts when policy allows redaction instead of block
4. **Sanitize output** — mutate completions

Each rail is its own dashboard Custom Guardrail Config. Validate rails map to `Operation: Validate`. Sanitize rails map to `Operation: Mutate`.

## Verdict mapping

Policy blocks must not use HTTP 4xx. On gateways after commit `a1c551be`, blocks are:

```json
HTTP 200
{"verdict": false, "message": "Knostic prompt-inspect-input: prompt_injection"}
```

Transient Knostic outages return 5xx from the wrapper. With **Fail on error: false**, the gateway can distinguish "rail said no" from "rail was down."

## Context forwarded to Knostic

The wrapper passes:

- OpenAI-format `messages` from `requestBody` or `responseBody`
- `messageType` of `PROMPT` or `COMPLETION`
- `sessionId` from config or gateway metadata (for conversation continuity)
- `userId` from `context.user.subjectSlug` or `subjectId`
- Optional `policyId` from env or dashboard config

That aligns with Knostic's need-to-know model: the same prompt may be allowed for one role and blocked for another.

## Deployment and verification

The integration ships with `deploy.py` for TrueFoundry Services and a standard Dockerfile for any other host. After deploy, `/debug/loaded-config` reports routes, API base, and `BUILD_REF` so you can confirm the pod is running the build you expect.

Tests mock Knostic HTTP responses for CI. Live tests opt in with `KNOSTIC_LIVE_TESTS=1`.

## What to test

Use prompts that discriminate guardrail behavior — soft jailbreaks and verbatim exfil markers — not prompts the base model already refuses. If rail-on and rail-off produce the same outcome, you may be measuring the model, not Knostic.

## Getting started

Clone the `integrations/knostic` directory from the [tfy-custom-guardrails](https://github.com/truefoundry/integrations-custom-guardrails) monorepo, obtain tenant API details from Knostic, deploy the wrapper, and register the four configs in your gateway dashboard. Step-by-step instructions are in `docs/public-docs-knostic.md` in the same folder.
