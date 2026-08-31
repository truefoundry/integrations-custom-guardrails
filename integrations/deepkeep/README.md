# DeepKeep Custom Guardrail Wrapper for tfy-llm-gateway

Thin FastAPI service that lets `tfy-llm-gateway`'s **Custom Guardrail**
integration call DeepKeep's real AI Firewall API.

## Why this exists

The gateway's custom guardrail contract and DeepKeep's actual API don't speak
the same schema, so this wrapper translates between them:

| | Gateway sends/expects | DeepKeep sends/expects |
|---|---|---|
| Input check | `InputGuardrailRequest` (`requestBody`, OpenAI chat shape) | `POST /api/v3/openai/moderations/pre` → `{"model": "<firewall_id>", "input": "..."}` |
| Output check | `OutputGuardrailRequest` (`requestBody` + `responseBody`) | `POST /api/v3/openai/moderations/post` → `{"model": "<firewall_id>", "output": "..."}` |
| Verdict | `null`=pass, modified body=mutate, `4xx`=block | `ApplyGuardrailResponse.flagged` + `verbosity[].details.guardrail_action` (`allow`/`alert`/`redact`/`modify`/`replace`/`block`) |

## 1. Configure DeepKeep

1. In the DeepKeep platform, create (or reuse) a **Firewall** and note its
   `firewall_id` — this is passed as `model` in every DeepKeep call.
2. Generate an API token: user icon → **API Management** → **Add New Token**.
   Copy it immediately (shown once).

## 2. Deploy the wrapper

```bash
pip install -r requirements.txt

export DEEPKEEP_BASE_URL="https://api.poc2.aws.deepkeep.ai"   # your DeepKeep API host
export DEEPKEEP_API_KEY="<your-deepkeep-token>"
export DEEPKEEP_INPUT_FIREWALL_ID="<firewall_id_for_input_checks>"
export DEEPKEEP_OUTPUT_FIREWALL_ID="<firewall_id_for_output_checks>"  # optional, defaults to input firewall
export WRAPPER_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

uvicorn main:app --host 0.0.0.0 --port 8080
```

Deploy this wherever the gateway can reach it (same VPC, or public HTTPS).
`WRAPPER_API_KEY` gates `/guardrails/*` and `/diagnose`; leave it unset only for
local development (auth disabled). `/healthz` stays open for probes.

Endpoints exposed:
- `POST /guardrails/input` — wire to the gateway's **Request**-target guardrail (bearer required)
- `POST /guardrails/output` — wire to the gateway's **Response**-target guardrail (bearer required)
- `GET /diagnose` — DeepKeep connectivity probe (bearer required)
- `GET /healthz` — health check (open)

## 3. Wire it into the gateway dashboard

**AI Gateway → Guardrails → Add New Guardrails Group → Custom Guardrail Config**

For the input check:
- **Name** — `deepkeep-input-firewall`
- **Operation** — `Mutate` (it can return a modified body)
- **URL** — `https://<your-wrapper-host>/guardrails/input`
- **Auth Data** — Custom Bearer Auth with the same value as `WRAPPER_API_KEY`
- **Target** — `Request`
- **Enforcing Strategy** — `Enforce` (recommended) or `audit` while testing

Repeat for the output check pointing at `/guardrails/output`, **Target: Response**.

Attach both to your model / virtual model.

## 4. Test

```bash
curl -X POST "https://<gateway-host>/api/gateway/chat/completions" \
  -H "Authorization: Bearer <tfy-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"model": "<virtual-model>", "messages": [{"role": "user", "content": "My SSN is 123-45-6789"}]}'
```

Expected: the wrapper calls DeepKeep's `/moderations/pre`, DeepKeep flags PII
with `guardrail_action: replace` (or `redact` / `modify`), the wrapper returns
the masked message body, and the gateway forwards the **redacted** prompt to
the upstream LLM. If a mutate action wins but DeepKeep omits usable
`modified` text, or returns an unrecognized action, the wrapper **denies**
rather than pass-throughing the original (unredacted) content.

## Notes / things to double check with DeepKeep before going to production

- Confirm whether `DEEPKEEP_INPUT_FIREWALL_ID` and `DEEPKEEP_OUTPUT_FIREWALL_ID`
  should really be the same firewall or two separately configured ones —
  DeepKeep's firewall config determines which guardrails run, so this is a
  product decision, not just wiring.
- `latency_ms` and `request_id` from DeepKeep's response are logged in this
  wrapper's block responses for traceability — pipe them into your own logs/
  APM if you want end-to-end correlation with the gateway's own request IDs.
- This wrapper only inspects the **last** user/assistant message. If you need
  full-conversation moderation, pass `input`/`output` as a list built from
  the full message history instead (DeepKeep's schema accepts
  `string | list[string]`).

## Deploying to TrueFoundry

This deploys the wrapper as a **standalone TrueFoundry Service**, separate from
the AI Gateway. The gateway then calls it as an external Custom Guardrail URL.

1. Install the deploy-time SDK (kept out of runtime `requirements.txt`):

   ```bash
   pip install -U truefoundry
   # or: pip install -r requirements-deploy.txt
   ```

2. Create TrueFoundry secrets under the **Secrets** tab for:
   - `DEEPKEEP_API_KEY` — set `DEEPKEEP_API_KEY_TFY_SECRET=tfy-secret://…` in `.env`
   - `WRAPPER_API_KEY` — set `WRAPPER_API_KEY_TFY_SECRET=tfy-secret://…` in `.env`
     (or put the raw value in `WRAPPER_API_KEY`; prefer a secret FQN). Never put
     a raw API key in `deploy.py`.

3. Fill in the other `DEEPKEEP_*` placeholders in `.env`
   (`DEEPKEEP_BASE_URL`, `DEEPKEEP_INPUT_FIREWALL_ID`,
   `DEEPKEEP_OUTPUT_FIREWALL_ID`).

4. Find your `workspace_fqn` from the TrueFoundry dashboard **Workspaces** tab.

5. Deploy:

   ```bash
   python deploy.py --workspace_fqn <your-workspace-fqn>
   ```

6. After deploy, copy the public service endpoint from the **Deployments**
   dashboard. That becomes the base URL for the two gateway Custom Guardrail
   configs:
   - `<endpoint>/guardrails/input`
   - `<endpoint>/guardrails/output`

Configure the gateway Custom Guardrail **Auth Data** with the same bearer as
`WRAPPER_API_KEY`. The three must match: TFY secret → pod env → dashboard
Custom Bearer Auth field. `/healthz` remains unauthenticated for probes;
`/guardrails/*` and `/diagnose` require the bearer.
