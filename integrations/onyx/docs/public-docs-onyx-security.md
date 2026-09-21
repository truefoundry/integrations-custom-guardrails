# Onyx Security

> Onyx AI Guard on TrueFoundry AI Gateway via the dedicated `/truefoundry` evaluate endpoint.

Point Custom Guardrails **directly** at Onyx. No adapter or FastAPI wrapper is required — Onyx speaks the TrueFoundry custom-guardrail contract natively.

## What is Onyx Security?

Onyx AI Guard evaluates LLM prompts and responses against policies you configure in the Onyx console (prompt protection, content moderation, keywords, sensitive data, and more). TrueFoundry calls Onyx on the `llm_input` and `llm_output` hooks and enforces the returned `verdict`.

## How it works

1. Build the evaluate URL from your AI Guard policy:

   ```
   https://<routing-id>.ai-guard.onyx.security/guard/evaluate/v1/<guard-token>/truefoundry
   ```

   The Guard Token in the path is the auth to Onyx (leave Auth Data empty in the dashboard). Use your tenant hostname — bare `https://ai-guard.onyx.security` is not routed and returns 404.

2. Register two Custom Guardrail configs (input + output) that both use that same URL.
3. TrueFoundry POSTs its standard custom-guardrail body. Presence of `responseBody` selects output evaluation.
4. Onyx returns HTTP 200 with `{"verdict": true}` or `{"verdict": false, "message": "..."}`. Mask/ask map to a block verdict on this Validate integration.

## Response contract

| HTTP | Body | Meaning |
|---|---|---|
| `200` | `{"verdict": true}` | Allow |
| `200` | `{"verdict": false, "message": "..."}` | Block (policy) |

Set **Enforcing Strategy** to Enforce or Enforce But Ignore On Error for block testing. Audit logs violations without blocking. Streamed responses skip output guardrails — use `"stream": false` when verifying output.

## Prerequisites

- TrueFoundry AI Gateway that honors `verdict: false` on HTTP 200.
- An Onyx AI Guard policy with at least one **Block** rule that scans Input and/or Output.
- Outbound HTTPS from the gateway to your tenant AI Guard host.

## Setup

### 1. Get your Guard Token

In Onyx: **Policies → Runtime Policies → AI Guard → ⋮ → AI Guard URL**. Copy the Guard Token and the tenant hostname. This is not an MCP Gateway Token or an API Keys page credential.

### 2. Register Custom Guardrail configs

AI Gateway → Guardrails → New Guardrails Group → Custom → group name `onyx-security`.

| Field | Input config | Output config |
|---|---|---|
| Name | `onyx-input` | `onyx-output` |
| Operation | Validate | Validate |
| Target | Request | Response |
| URL | Full `/truefoundry` URL above | Same |
| Auth Data | None (token is in the URL path) | Same |
| Enforcing Strategy | Enforce But Ignore On Error | Same |
| Config | `{}` | `{}` |

### 3. Attach to traffic

Model pin, gateway policy, or per-request header:

```json
{
  "llm_input_guardrails": ["onyx-security/onyx-input"],
  "llm_output_guardrails": ["onyx-security/onyx-output"]
}
```

## Verify

**Input.** Send each blocked keyword as the user message in a separate request (`bradpitt`, `fightclub`, `norton` on the Onyx test policy). Expect HTTP 400 with `error.type: guardrail_checks_failed` under Enforce / Enforce But Ignore On Error.

**Output.** Attach `onyx-output`, set `"stream": false`, and keep the keyword out of the input. For example ask the model to join spaced letters so the assistant produces `bradpitt`. Expect the response to be blocked. Repeat for `fightclub` and `norton`.

Confirm the violation in Onyx Runtime Alerts and the guardrail result in TrueFoundry Request Traces.

Direct curl against Onyx (same body the gateway sends). Include `subjectSlug` on
`context.user` — without it Onyx returns `verdict: false` with
`"Onyx AI Guard could not validate the request"`:

```bash
curl -sS -X POST \
  "https://<routing-id>.ai-guard.onyx.security/guard/evaluate/v1/<guard-token>/truefoundry" \
  -H "Content-Type: application/json" \
  -d '{
    "requestBody": {"model": "openai/gpt-4o", "stream": false, "messages": [{"role": "user", "content": "What is the weather today?"}]},
    "context": {"user": {"subjectId": "u1", "subjectType": "user", "subjectSlug": "u1"}},
    "config": {}
  }'
```

## Known limitations

- **Validate only** — Mask rules return a block verdict instead of redacting content.
- **No streamed output guarding** — set `"stream": false` for response checks.
- **Direction matters** — Input-only rules do not block responses; enable Output scanning on the rule.
- **Text only** — latest user message (input) / first assistant message (output); tool/image rules have nothing to evaluate on this path.

## Optional local forwarder

`integrations/onyx/` also ships a small FastAPI forwarder that POSTs the same TrueFoundry body to `/truefoundry`. Production should call Onyx directly. Use the forwarder only for local smoke tests (`pytest -v tests/` with `ONYX_API_KEY` + `ONYX_API_BASE` set).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Traffic not guarded | Group not attached; or strategy is Audit |
| Guardrail non-200 | Wrong tenant host, wrong Guard Token path segment, or policy inactive |
| `verdict: false` with "could not validate the request" | Missing `context.user.subjectSlug` (required by `/truefoundry`) |
| Input blocks but output allows | Output config missing, rule has no Output scan, or `"stream": true` |
| Mask content blocked instead of redacted | Expected on Validate — see Known limitations |
| Gateway allows despite `verdict: false` | Use Enforce / Enforce But Ignore On Error; confirm gateway supports verdict-on-200 |

## Reference

| Item | Value |
|---|---|
| Source repo | `truefoundry/integrations-custom-guardrails/integrations/onyx` |
| Onyx platform | [onyx.security](https://onyx.security) |
| Evaluate path | `/guard/evaluate/v1/<guard-token>/truefoundry` |
| API base | `https://<routing-id>.ai-guard.onyx.security` |
| Selector | `onyx-security/<config-name>` |
