# Lasso Security

Use [Lasso Security](https://server.lasso.security) as an input and output guardrail on the TrueFoundry AI Gateway. This integration runs Lasso API v3 `classify` and `classifix` inside a small wrapper service that you deploy on TrueFoundry. The gateway calls the wrapper through its Custom Guardrail interface. Policy rules and deputies are configured in the Lasso console; the wrapper forwards traffic and maps responses to the gateway contract.

> **Source**: [`truefoundry/integrations-custom-guardrails/integrations/lasso-security/`](https://github.com/truefoundry/integrations-custom-guardrails/tree/main/integrations/lasso-security). Open the folder for the Dockerfile, deploy script, and handler code.

## What this gives you

- **Validate rails** (`classify`) — block requests or responses when Lasso deputies return `action: BLOCK`.
- **Mutate rails** (`classifix`) — redact PII in place when Lasso returns mask spans or rewritten messages; still block when a violation cannot be masked.
- **Input and output** — separate endpoints for pre-call and post-call hooks.
- **Session continuity** — optional `sessionId` / `userId` forwarded to Lasso for conversation-aware policy.
- **Agent attribution** — optional `agentId` / `agentName` so Lasso can report findings per agent.

Deputy behavior (what triggers BLOCK vs WARN vs mask) is defined in your Lasso account, not in this repo.

## Prerequisites

- A TrueFoundry workspace you can deploy services into.
- A **Lasso API key** from https://server.lasso.security.
- Deputies configured in the Lasso console for the policies you want enforced.
- The model FQN you want to protect (e.g. `openai-main/gpt-4o-mini`).
- A cluster with a configured base host (visible at **Integrations → Clusters → \<cluster\>**).

## Architecture in one paragraph

The gateway calls the wrapper at `llm_input` and/or `llm_output` hooks. The wrapper extracts messages from the OpenAI-shaped `requestBody` or `responseBody`, POSTs to Lasso API v3 (`classify` or `classifix`), and returns `HTTP 200` with a verdict JSON body. On validate rails, `BLOCK` findings become `{"verdict": false}`; everything else passes. On mutate rails, masked content is returned as `{"verdict": true, "transformed": true, "result": {...}}` when Lasso supplies masks; hard blocks still return `verdict: false`.

## Step 1 — Deploy the wrapper service

Clone the integration repo:

```bash
git clone https://github.com/truefoundry/integrations-custom-guardrails
cd integrations-custom-guardrails/integrations/lasso-security
```

Copy `.env.example` to `.env` and fill in the values:

```
LASSO_API_KEY=<your Lasso API key>
WRAPPER_API_KEY=<generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`>

TFY_WORKSPACE_FQN=<cluster>:<workspace>
TFY_PUBLIC_HOST=ml.<cluster>.truefoundry.cloud
TFY_PUBLIC_PATH=/lasso-guardrails-tfy/

LASSO_API_KEY_SECRET_FQN=tfy-secret://<workspace>/lasso-guardrails-tfy/lasso-api-key
WRAPPER_API_KEY_SECRET_FQN=tfy-secret://<workspace>/lasso-guardrails-tfy/wrapper-api-key
```

Create the two secrets in the dashboard before deploying (see Step 2 below).

Deploy:

```bash
pip install -U truefoundry
tfy login
python deploy.py --wait
```

Verify the service is healthy:

```bash
curl -s https://ml.<cluster>.truefoundry.cloud/lasso-guardrails-tfy/health
# {"status":"ok"}
```

## Step 2 — Create the two secrets

Navigate to **Platform → Secrets → + Secret Group `lasso-guardrails-tfy`** and create two secrets:

| Name | Value |
|---|---|
| `lasso-api-key` | Your Lasso API key. Injected as `LASSO_API_KEY` in the pod. |
| `wrapper-api-key` | The same random string you put in `.env` as `WRAPPER_API_KEY`. The gateway sends this as `Authorization: Bearer ...` when calling the wrapper. |

Copy each secret's FQN and ensure it matches the corresponding entry in `.env`. Redeploy if you updated `.env` after the first deploy.

## Step 3 — Register the Custom Guardrail configs

Navigate to **AI Gateway → Guardrails → + Add New Guardrails Group**.

1. **Group name**: `lasso-security` (or your preferred bundle name)
2. Description (optional): `Lasso Security classify + classifix`
3. Click **+ Add Guardrail Config → Custom Guardrail Config** **four times** — one per rail.

### Per-rail configs

| Name | Operation | URL suffix |
|---|---|---|
| `lasso-classify-input` | **Validate** | `/lasso-classify` |
| `lasso-classify-output` | **Validate** | `/lasso-classify-output` |
| `lasso-classifix-input` | **Mutate** | `/lasso-classifix` |
| `lasso-classifix-output` | **Mutate** | `/lasso-classifix-output` |

For each config:

| Field | Value |
|---|---|
| URL | `https://ml.<cluster>.truefoundry.cloud/lasso-guardrails-tfy/<suffix>` |
| Auth Data | **Custom Bearer Auth**, token = the `wrapper-api-key` secret value |
| Headers | (empty) |
| Config | `{}` — or `{"credentials": {"apiKey": "..."}}` to override the deploy secret per config |
| **Fail on error** | **`false`** |

Save the group.

### Optional: tag calls with an agent identity

If you run more than one agent behind the gateway, tell Lasso which one produced each inference so findings can be attributed. Both fields are optional and independent, and neither changes a verdict.

Set a service-wide default in `.env` before deploying:

```
LASSO_AGENT_ID=support-bot-prod
LASSO_AGENT_NAME=Support Bot
```

Override for a single guardrail config with the Config JSON field:

```json
{"agentId": "support-bot-prod", "agentName": "Support Bot"}
```

Override per request by setting `agent_id` / `agent_name` (or `lasso-agent-id` / `lasso-agent-name`) in the gateway's request metadata. Per-request metadata wins over the Config JSON value, which wins over the deploy env — most specific source first.

Keep each value under 128 characters with no control characters — Lasso rejects the call otherwise. The wrapper trims values and drops an invalid one rather than losing the scan, so a missing `agentId` in the Lasso console usually means the value was malformed.

### About Fail on error

With gateway commit `a1c551be` or later, rail blocks are `HTTP 200` + `verdict: false`. Real wrapper or Lasso outages return 5xx. **`Fail on error: false`** lets the gateway distinguish "policy blocked" from "guardrail unavailable." Use `true` only when you want transient Lasso outages to fail closed.

## Step 4 — Attach guardrails to a model

**Dashboard pin**: **AI Gateway → Models → \<model\> → Guardrails** tab → attach the `lasso-security` group → save.

**Per-request header**:

```python
extra_headers={"X-TFY-GUARDRAILS": json.dumps({
    "llm_input_guardrails":  ["lasso-security/lasso-classify-input"],
    "llm_output_guardrails": ["lasso-security/lasso-classify-output"],
})}
```

Use the mutate config names instead when you want PII masking rather than hard blocks.

## Step 5 — Test

Validate rail block (depends on your Lasso deputy config; example if PII deputy blocks email):

```bash
curl -sS -X POST https://ml.<cluster>.truefoundry.cloud/lasso-guardrails-tfy/lasso-classify \
  -H "Authorization: Bearer <wrapper-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "requestBody": {"messages": [{"role": "user", "content": "My email is jane@example.com"}]},
    "context": {"user": {"subjectSlug": "test-user"}}
  }'
```

Expected when Lasso BLOCKs: `{"verdict": false, "message": "Lasso guardrail blocked: ..."}`.

Post-deploy verification:

```bash
curl -sS https://ml.<cluster>.truefoundry.cloud/lasso-guardrails-tfy/debug/runtime-config \
  -H "Authorization: Bearer <wrapper-api-key>"
```

Check `wrapper_version`, `lasso_api_key_configured: true`, and the `routes` map.

## Troubleshooting

### Gateway returns 401 from the wrapper

The three-way bearer sync must match:

1. TFY secret `lasso-guardrails-tfy/wrapper-api-key`
2. Pod env `WRAPPER_API_KEY` (via secret reference in `deploy.py`)
3. Dashboard Custom Bearer Auth field on each Custom Guardrail Config

### "Lasso API key not configured"

Set `lasso-api-key` in the secret group and redeploy, or pass `{"credentials": {"apiKey": "..."}}` in the dashboard Config JSON.

### Violations in Lasso console but gateway allows

Only findings with `action: BLOCK` deny on validate rails. `WARN`-only findings are logged and allowed. Adjust deputy severity in the Lasso console if you need hard blocks.

### Mutate rail blocks instead of masking

`classifix` can only mask findings that include span metadata (`start`, `end`, `mask`) or rewritten messages from Lasso. BLOCK findings without mask data still deny. Use validate rails when you want hard stops regardless of maskability.

## Reference

| Item | Value |
|---|---|
| Default Lasso API base | `https://server.lasso.security/gateway/v3` |
| Wrapper service name | `lasso-guardrails-tfy` |
| Agent identity limit | 128 characters, no control characters |
| Debug endpoint | `GET /debug/runtime-config` (bearer auth) |
| Selector format | `lasso-security/lasso-classify-input`, etc. |
| Repo | [`truefoundry/integrations-custom-guardrails/integrations/lasso-security/`](https://github.com/truefoundry/integrations-custom-guardrails/tree/main/integrations/lasso-security) |
| Upstream | [Lasso Security](https://server.lasso.security) |
