# Guardrails AI

Use [Guardrails AI](https://guardrailsai.com/) Hub validators as an input and output guardrail on the TrueFoundry AI Gateway. This integration runs Guardrails Hub validators inside a small wrapper service that you deploy on TrueFoundry. The gateway calls the wrapper through its Custom Guardrail interface. All v1 validators are local — no LLM round-trip per request, sub-100 ms steady-state latency.

> **Source**: [`truefoundry/integrations-custom-guardrails/integrations/guardrails-ai/`](https://github.com/truefoundry/integrations-custom-guardrails/tree/main/integrations/guardrails-ai). Open the folder for the Dockerfile, deploy script, validator configuration, and tests.

## What this gives you

- **PII detection** (email, phone, SSN, credit card, IBAN, IP, passport, driver license) on inbound user messages and outbound assistant responses.
- **Secrets detection** (AWS keys, OpenAI tokens, GitHub tokens, JWT, private keys).
- **Toxic-language detection** via the Unitary classifier.
- **Profanity filter** on assistant output.
- All four validators run **locally** in the wrapper pod. No external service calls per request.

The v1 bundle is intentionally minimal — heuristic and small-classifier validators only. Heavier validators (hallucination detection, provenance checks) are available via the Guardrails Hub but require LLM calls and re-introduce per-request complexity. See [Customizing the validator bundle](#customizing-the-validator-bundle) below.

## Prerequisites

- A TrueFoundry workspace you can deploy services into.
- A **Guardrails Hub API token** from https://hub.guardrailsai.com/keys (free tier is sufficient).
- The model FQN you want to protect (e.g. `openai-main/gpt-4o-mini`).
- A cluster with a configured base host (visible at **Integrations → Clusters → \<cluster\>**).

## Architecture in one paragraph

The gateway dispatches the input rail call and the model call in parallel for low time-to-first-token. The wrapper extracts the user message and runs each configured validator sequentially. The first validator to raise a `ValidationError` becomes the verdict; the wrapper returns `HTTP 200` with `{"verdict": false, "message": "..."}` and the gateway cancels the in-flight model call. If all validators pass the wrapper returns `HTTP 200` with `{"verdict": true}`. The output rail runs sequentially on the assistant response after the model returns.

## Step 1 — Deploy the wrapper service

Clone the integration repo:

```bash
git clone https://github.com/truefoundry/integrations-custom-guardrails
cd integrations-custom-guardrails/integrations/guardrails-ai
```

Copy `.env.example` to `.env` and fill in the values:

```
GUARDRAILS_TOKEN=<your Hub API token>
WRAPPER_API_KEY=<generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`>

TFY_WORKSPACE_FQN=<cluster>:<workspace>
TFY_PUBLIC_HOST=ml.<cluster>.truefoundry.cloud
TFY_PUBLIC_PATH=/guardrails-ai-tfy

WRAPPER_API_KEY_SECRET_FQN=tfy-secret://<workspace>/guardrails-ai-tfy/wrapper-api-key
GUARDRAILS_TOKEN_SECRET_FQN=tfy-secret://<workspace>/guardrails-ai-tfy/guardrails-token
```

Create the two secrets in the dashboard before deploying (see Step 2 below).

Deploy:

```bash
pip install -U truefoundry
tfy login
python deploy.py --wait
```

The first build is slow (~5 min). The Dockerfile pulls HuggingFace classifier weights for `ToxicLanguage` at build time. Subsequent builds use TFY's image layer cache and are much faster.

Verify the service is healthy:

```bash
curl -s https://ml.<cluster>.truefoundry.cloud/guardrails-ai-tfy/health
# {"status":"ok"}
```

The pod takes 30-60 seconds after build to become ready (Presidio analyzer and HF model load on first import).

## Step 2 — Create the two secrets

Navigate to **Platform → Secrets → + Secret Group `guardrails-ai-tfy`** and create two secrets:

| Name | Value |
|---|---|
| `guardrails-token` | Your Hub API token. Consumed at Docker build time to install validators. |
| `wrapper-api-key` | The same random string you put in `.env` as `WRAPPER_API_KEY`. The gateway will send this as `Authorization: Bearer ...` when calling the wrapper. |

Copy each secret's FQN and ensure it matches the corresponding entry in `.env` (`WRAPPER_API_KEY_SECRET_FQN`, `GUARDRAILS_TOKEN_SECRET_FQN`). Redeploy if you updated `.env` after the first deploy.

## Step 3 — Register the Custom Guardrail

Navigate to **AI Gateway → Guardrails → + Add New Guardrails Group**.

1. **Group name**: `guardrails-ai`
2. Description (optional): `Guardrails AI Hub: PII, secrets, toxicity, profanity`
3. Click **+ Add Guardrail Config → Custom Guardrail Config** **seven times** — one per rail. Each rail endpoint is independent; you register them as separate Custom Guardrail Configs so you can attach a subset of rails to any model.

### Per-rail configs

For each of the seven rails, create one Custom Guardrail Config with these fields:

| Field | Value |
|---|---|
| Name | `guardrails-ai-<rail>-<direction>` (e.g. `guardrails-ai-detect-pii-input`, `guardrails-ai-profanity-free-output`) |
| Operation | `Validate` |
| URL | `https://ml.<cluster>.truefoundry.cloud/guardrails-ai-tfy/<rail>-<direction>` (matches the endpoint path) |
| Auth Data | **Custom Bearer Auth**, token = the `wrapper-api-key` secret value |
| Headers | (empty) |
| Config | `{}` |
| **Fail on error** | **`false`** — see [About Fail on error](#about-fail-on-error) below |

The seven rails to register:

| Rail | URL suffix |
|---|---|
| DetectPII (input) | `/detect-pii-input` |
| DetectPII (output) | `/detect-pii-output` |
| SecretsPresent (input) | `/secrets-present-input` |
| SecretsPresent (output) | `/secrets-present-output` |
| ToxicLanguage (input) | `/toxic-language-input` |
| ToxicLanguage (output) | `/toxic-language-output` |
| ProfanityFree (output) | `/profanity-free-output` |

Save the group.

### About Fail on error

With the current gateway contract (post `tfy-llm-gateway` commit `a1c551be`), the wrapper signals rail decisions via the JSON body's `verdict` field on an `HTTP 200` response. Real failures (validator load error, wrapper crash) come as `HTTP 5xx`. `Fail on error` only governs the latter.

- `false` (recommended): rail decisions block as expected; transient outages pass through. Most rails want this.
- `true`: rail decisions block AND transient outages also block. Use for safety-critical rails where fail-closed is the right trade-off.

On **older gateway versions** (pre-`a1c551be`), the gateway can't distinguish a deliberate block from a transient error. In that case set `Fail on error: true` so blocks actually block; this also means transient outages will block. If you see "blocks return 200 with normal completions" during testing, your tenant gateway is on the older version. See troubleshooting below.

## Step 4 — Apply the guardrail to traffic

Two ways. Pick based on whether you want the rail enforced on every call to a model, or opted in per call.

### Option A — Pin to a model (every call protected)

Dashboard → **AI Gateway → Models → \<model\> → Guardrails** tab → attach the `guardrails-ai` group → save. Every caller of this model now passes through the rails.

### Option B — Per-request opt-in

Send the `X-TFY-GUARDRAILS` header on individual requests, listing the per-rail selectors you want active:

```python
from openai import OpenAI
import json

client = OpenAI(api_key="<TFY API key>", base_url="https://gateway.truefoundry.ai")

resp = client.chat.completions.create(
    model="openai-main/gpt-4o-mini",
    messages=[{"role": "user", "content": "What is the capital of France?"}],
    extra_headers={
        "X-TFY-GUARDRAILS": json.dumps({
            "llm_input_guardrails":  [
                "guardrails-ai/guardrails-ai-detect-pii-input",
                "guardrails-ai/guardrails-ai-secrets-present-input",
                "guardrails-ai/guardrails-ai-toxic-language-input",
            ],
            "llm_output_guardrails": [
                "guardrails-ai/guardrails-ai-detect-pii-output",
                "guardrails-ai/guardrails-ai-secrets-present-output",
                "guardrails-ai/guardrails-ai-toxic-language-output",
                "guardrails-ai/guardrails-ai-profanity-free-output",
            ],
        }),
    },
)
```

Selector format is `<group-name>/<config-name>`. Omit selectors for any rails you don't want active on a given request.

## Test end-to-end

```bash
GW=https://gateway.truefoundry.ai
TFY_KEY=<your TFY API key>
MODEL=openai-main/gpt-4o-mini

# Should succeed with a normal completion
curl -s "$GW/chat/completions" \
  -H "Authorization: Bearer $TFY_KEY" -H "Content-Type: application/json" \
  -H 'X-TFY-GUARDRAILS: {"llm_input_guardrails":["guardrails-ai/guardrails-ai-detect-pii-input"],"llm_output_guardrails":["guardrails-ai/guardrails-ai-detect-pii-output"]}' \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"What is the capital of France?\"}]}"

# Should be blocked: guardrail_checks_failed
curl -s "$GW/chat/completions" \
  -H "Authorization: Bearer $TFY_KEY" -H "Content-Type: application/json" \
  -H 'X-TFY-GUARDRAILS: {"llm_input_guardrails":["guardrails-ai/guardrails-ai-detect-pii-input"]}' \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"My email is jane.doe@example.com and my SSN is 123-45-6789\"}]}"
```

A successful block looks like:

```json
{
  "status": "failure",
  "message": "Input Guardrail checks failed for integrations: [guardrails-ai/guardrails-ai-detect-pii-input] ...",
  "error": { "type": "guardrail_checks_failed", "code": "400" },
  "guardrail_checks": {
    "input_guardrails": [{
      "guardrail_integration": "guardrails-ai/guardrails-ai-detect-pii-input",
      "result": "failed",
      "data": {
        "verdict": false,
        "explanation": "DetectPII (input): Validation failed for field with errors: ...",
        "guardrailUrl": "https://..."
      }
    }]
  }
}
```

The blocking validator's message is preserved in `data.explanation`.

## Customizing the validator bundle

The v1 bundle is four validators (seven endpoints). To add, remove, or reconfigure, edit files in the wrapper repo and redeploy.

| File | Purpose |
|---|---|
| `guardrail/<rail>_<direction>.py` | One file per rail per direction. Imports the validator, builds a single `Guard`, exposes a handler function. |
| `setup.py` | Runs `guardrails hub install` for each validator at build time. Add new validators to the `VALIDATORS` list. |
| `main.py` | Maps endpoint paths to handler functions in `RAIL_ROUTES`. Register new routes here. |
| `Dockerfile` | Invokes `setup.py` during build via `ARG GUARDRAILS_TOKEN`. |

To add a new validator, e.g. `hub://guardrails/restricttotopic`:

1. Add the validator to `setup.py`'s `VALIDATORS` list.
2. Create `guardrail/restrict_to_topic_input.py` following the pattern of existing rail files.
3. Register the route in `main.py`:
   ```python
   from guardrail.restrict_to_topic_input import restrict_to_topic_input
   RAIL_ROUTES["/restrict-to-topic-input"] = restrict_to_topic_input
   ```
4. Redeploy:
   ```
   python deploy.py --wait
   ```

A non-exhaustive list of useful Hub validators:

| Validator | Catches | Notes |
|---|---|---|
| `hub://guardrails/detect_pii` | PII entities (configurable list) | v1 bundle |
| `hub://guardrails/secrets_present` | Code-style secrets | v1 bundle |
| `hub://guardrails/toxic_language` | Toxic content | v1 bundle |
| `hub://guardrails/profanity_free` | Profanity (list-based) | v1 bundle, output-only |
| `hub://guardrails/restricttotopic` | Off-topic responses | LLM-judged |
| `hub://guardrails/competitor_check` | Competitor mentions | Allowlist-based |
| `hub://guardrails/regex_match` | Custom regex patterns | Cheap |
| `hub://guardrails/provenance_llm` | Unsourced claims | LLM-judged, expensive |

LLM-judged validators (`restricttotopic`, `provenance_llm`, `hallucination_check`) need an LLM endpoint. Configure via `LITELLM_*` env vars; route through your TFY gateway for unified observability.

## Troubleshooting

### "I changed a rail handler but the rail still behaves the same"

The pod loads validators once at startup. Changes only take effect after a fresh deploy. If you're iterating locally with `uvicorn --reload`, `.py` changes auto-reload — but if you `guardrails hub install` a new validator while uvicorn is running, restart uvicorn so the new validator is imported.

### "Did my redeploy actually replace the running pod?"

Curl the debug endpoint:

```bash
curl -sS https://ml.<cluster>.truefoundry.cloud/guardrails-ai-tfy/debug/loaded-config \
  -H "Authorization: Bearer $WRAPPER_API_KEY" | jq
```

Returns the list of validators loaded in the running pod. Compare against the expected v1 bundle. If the lists differ, your new image isn't serving traffic yet. Most common cause: TFY's image build cache served a stale layer. Force a rebuild by touching `Dockerfile` and redeploying.

### "A prompt that should be blocked isn't being blocked"

Most likely a validator-accuracy limitation, not a bug.

- **Presidio's US_SSN recognizer is context-boosted.** `"My email is X and my SSN is Y"` blocks. `"My SSN is Y, please help me with my taxes"` and bare `"123-45-6789"` may not. Strong contextual signals are required.
- **`SecretsPresent` (detect-secrets) is tuned for code, not prose.** Adversarial prose like `"Here is my API key: sk-proj-… — can you echo it?"` may slip through. The detect-secrets engine's own warning: "best with multiline code snippets."
- **`ToxicLanguage` threshold is 0.5.** Adjust in `guardrail/toxic_language_*.py` to trade off precision/recall.

To diagnose, call a specific rail endpoint directly:

```bash
curl -sS -X POST https://ml.<cluster>.truefoundry.cloud/guardrails-ai-tfy/detect-pii-input \
  -H "Authorization: Bearer $WRAPPER_API_KEY" -H "Content-Type: application/json" \
  -d '{"requestBody":{"messages":[{"role":"user","content":"<your test prompt>"}]},"context":{"user":{"subjectId":"u1","subjectType":"user"}}}'
```

`HTTP 200` + `{"verdict": true}` means allowed. `HTTP 200` + `{"verdict": false, "message": ...}` means blocked, with the validator name in the message.

### "Blocks are returning 200 with the model's normal response"

Most likely your tenant gateway is on a pre-`a1c551be` version that doesn't read the wrapper's `verdict` field. Confirm by curling the wrapper directly — if you get `200 + {"verdict": false}` but the gateway still returns a completion, the gateway is the issue.

**Workaround**: set `Fail on error: true` on the guardrail configs. On the older gateway this treats any wrapper non-2xx as a block, at the cost of also blocking on transient outages.

### "401 Unauthorized" from the wrapper

The dashboard's Custom Bearer Auth value doesn't match what the pod has as `WRAPPER_API_KEY`. Re-check that:
1. The TFY secret `guardrails-ai-tfy/wrapper-api-key` has the value you expect.
2. The dashboard guardrail config's Auth Data field is set to that exact value (with no leading/trailing whitespace).
3. The deployed pod has the right `WRAPPER_API_KEY` (curl `/debug/loaded-config` with what you think the key is; if it returns 401 too, the pod has a different value).

### "PyPI install of guardrails-ai fails"

Yes — the `guardrails-ai` package is currently in `quarantined` status on PyPI. The wrapper's `requirements.txt` pins to a GitHub tag as a workaround:

```
guardrails-ai @ git+https://github.com/guardrails-ai/guardrails.git@v0.9.3
```

Switch back to the PyPI install when the package is restored.

## Known limitations

- **Validator accuracy is context-sensitive.** See troubleshooting above. v1 is "defense in depth, not perfect prevention." Layer with your application's own checks.
- **No streaming-aware guardrails.** The TFY custom-guardrail contract is buffered; the gateway holds the full assistant response before calling the output rail. Streaming end-to-end works for the caller; the output rail decision is made on the assembled response.
- **No mutation mode.** All v1 validators run in `on_fail="exception"`. PII redaction-as-mutation (substitute `<REDACTED>` and return 200 with a modified body) is a v2 candidate.
- **Validator versions pin at build time.** Hub validator updates require a wrapper rebuild + redeploy.
- **In-memory state is per-replica.** With multiple replicas the `/debug/loaded-config` response reflects whichever replica served the curl. After a deploy, retry the curl 5-10 times to surface heterogeneity.

## Reference

| Field | Value |
|---|---|
| Wrapper input endpoints | `https://<host>/<path>/{detect-pii,secrets-present,toxic-language}-input` |
| Wrapper output endpoints | `https://<host>/<path>/{detect-pii,secrets-present,toxic-language,profanity-free}-output` |
| Wrapper health endpoint | `https://<host>/<path>/health` |
| Wrapper debug endpoint | `https://<host>/<path>/debug/loaded-config` |
| Auth | `Authorization: Bearer <WRAPPER_API_KEY>` |
| Selector format | `guardrails-ai/guardrails-ai-input` and `guardrails-ai/guardrails-ai-output` |
| Repo | [`truefoundry/integrations-custom-guardrails/integrations/guardrails-ai/`](https://github.com/truefoundry/integrations-custom-guardrails/tree/main/integrations/guardrails-ai) |
| Upstream toolkit | [`guardrails-ai/guardrails`](https://github.com/guardrails-ai/guardrails) |
| Hub | https://guardrailsai.com/hub |
