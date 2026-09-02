# Onyx Security

> Onyx AI Guard on TrueFoundry AI Gateway via a deployable FastAPI wrapper.

Deploy the `integrations/onyx` FastAPI wrapper on any public HTTPS host. The AI Gateway calls it at `llm_input` / `llm_output` via the Custom Guardrail contract; the wrapper forwards extracted text to Onyx AI Guard `/simple` and returns `verdict` JSON on HTTP 200.

## What is Onyx Security?

Onyx AI Guard is a SaaS platform for evaluating LLM prompts and responses against policies you configure in the Onyx console. You define Input- and Output-direction rules there; the wrapper does not embed policy logic.

This integration uses Onyx's `/simple` evaluate API:

| Onyx API | Purpose | Gateway operation |
|---|---|---|
| `POST .../guard/evaluate/v1/{policy-token}/simple` | Score prompt or response; return `allow` / `block` / `modify` | Validate |

Auth to Onyx is the **policy token in the URL path** (`ONYX_API_KEY`) — there is no `Authorization` header on the Onyx call. The wrapper sends extracted text only: `{"user_prompt": "..."}` on input hooks and `{"response": "..."}` on output hooks (never both, never the whole gateway body).

v1 is validate-only. When Onyx returns `action: modify`, these rails block instead of rewriting content.

## How it works

1. The AI Gateway POSTs an OpenAI-shaped `requestBody` (input) or `requestBody` + `responseBody` (output) to your wrapper URL.
2. The wrapper extracts user/assistant text and calls Onyx `/simple` with your `ONYX_API_KEY` embedded in the evaluate URL.
3. The wrapper returns HTTP 200 with a policy outcome in the body (see below). Infrastructure failures return HTTP 5xx.

Onyx policy decisions are always HTTP 200 with an `action` field. `allow` becomes `{"verdict": true}`; `block` and `modify` become `{"verdict": false, "message": "..."}` (block copy comes from Onyx's `custom_popup_message`). Whether output blocking fires depends on your Onyx policy having an Output-direction rule.

## Response contract

| HTTP | Body | Meaning |
|---|---|---|
| `200` | `{"verdict": true}` | Allow |
| `200` | `{"verdict": false, "message": "..."}` | Block (policy) |
| `5xx` | error JSON | Wrapper or Onyx failure |

Policy blocks must use 2xx + `verdict: false`, not HTTP 4xx. See [Custom guardrail response contract](https://www.truefoundry.com/docs/ai-gateway/custom-guardrail-response-contract).

## Wrapper endpoints

| Path | Operation | Target |
|---|---|---|
| `/onyx-input` | Validate | Request (input) |
| `/onyx-output` | Validate | Response (output) |

`GET /health` — health check. `GET /debug/loaded-config` — bearer-gated deploy verification.

All POST routes expect `Authorization: Bearer <WRAPPER_API_KEY>` when the key is configured on the wrapper.

## Prerequisites

- Onyx policy token from the [Onyx Security](https://onyx.security) platform, plus Input / Output rules configured for your policies.
- Public HTTPS URL for the deployed wrapper.
- `WRAPPER_API_KEY` — shared secret; the AI Gateway sends it as `Authorization: Bearer …` when calling the wrapper.

## Setup

## Clone and configure

```bash
git clone https://github.com/truefoundry/integrations-custom-guardrails
cd integrations-custom-guardrails/integrations/onyx
cp .env.example .env
```

```bash
ONYX_API_KEY=<from https://onyx.security>
ONYX_API_BASE=https://<routing-id>.ai-guard.onyx.security
WRAPPER_API_KEY=<generate: python -c "import secrets; print(secrets.token_urlsafe(32))">
```

Get `ONYX_API_KEY` in the Onyx platform. Set `ONYX_API_BASE` to your tenant's AI Guard host (`https://<routing-id>.ai-guard.onyx.security`). The bare host `https://ai-guard.onyx.security` is not routed to any tenant and returns 404.

## Deploy the wrapper

Docker:

```bash
docker build -t onyx-guardrails-tfy .
docker run --rm -p 8000:8000 --env-file .env onyx-guardrails-tfy
```

Local:

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Put TLS in front of the service (load balancer, ingress, or your platform’s HTTPS URL). The AI Gateway must reach paths such as `https://<host>/onyx-input`.

## Deploy on TrueFoundry (optional)

Set `TFY_WORKSPACE_FQN`, `TFY_PUBLIC_HOST`, `TFY_PUBLIC_PATH`, and secret FQNs in `.env`. Create secrets `onyx-api-key` and `wrapper-api-key` under group `onyx-guardrails-tfy` in Platform → Secrets, then:

```bash
pip install -U truefoundry
tfy login
python deploy.py --wait
```

## Register Custom Guardrail configs

AI Gateway → Guardrails → + Add New Guardrails Group → type Custom.

- Group name: `onyx-security`
- Add one config per wrapper path (two total), or start with input validate only.

Input validate example:

| Field | Value |
|---|---|
| Name | `onyx-input` |
| Operation | Validate |
| Target | Request |
| Enforcing Strategy | Enforce |
| URL | `https://<host>/onyx-input` |
| Headers | `Authorization` → `Bearer <WRAPPER_API_KEY>` |
| Config | `{}` |

Register the remaining config:

| Name (example) | Operation | Target | Path |
|---|---|---|---|
| `onyx-output` | Validate | Response | `/onyx-output` |

Auth Data → Custom Bearer Auth works the same as Headers if you prefer not to set headers manually.

## Attach to traffic

Model pin: AI Gateway → Models → \<model\> → Guardrails → attach group `onyx-security`.

Per request — `X-TFY-GUARDRAILS` header, selector format `<group>/<config>`:

```json
{
  "llm_input_guardrails": ["onyx-security/onyx-input"],
  "llm_output_guardrails": ["onyx-security/onyx-output"]
}
```

## Verify

Call the wrapper directly:

```bash
curl -sS https://<host>/onyx-input \
  -H "Authorization: Bearer $WRAPPER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "requestBody": {"messages": [{"role": "user", "content": "What is the capital of France?"}]},
    "context": {"user": {"subjectSlug": "test-user"}}
  }'
```

Expect `{"verdict": false, ...}` when Onyx blocks, or `{"verdict": true}` when allowed (depends on your Onyx policy).

```bash
curl -sS https://<host>/debug/loaded-config -H "Authorization: Bearer $WRAPPER_API_KEY"
```

Confirm `onyx_api_key_configured: true` and the `routes` map.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `401` from wrapper | `WRAPPER_API_KEY` on the service does not match the dashboard Bearer token |
| `500` "Onyx API key not configured" | Missing `ONYX_API_KEY` and no `config.credentials.apiKey` override |
| Input blocks but output allows the same phrase | Onyx policy has an Input rule only; add an Output-direction rule |
| `modify` content still blocked | Validate rails fail-safe; masking needs a future Mutate rail |
| Gateway allows despite `verdict: false` | Tenant gateway not honoring verdict-on-200; set Enforce or upgrade gateway |

## Reference

| Item | Value |
|---|---|
| Source repo | `truefoundry/integrations-custom-guardrails/integrations/onyx` |
| Onyx platform | [onyx.security](https://onyx.security) (policy token) |
| Onyx API base | `https://<routing-id>.ai-guard.onyx.security` (required; bare `ai-guard.onyx.security` 404s) |
| Selector | `onyx-security/<config-name>` |
