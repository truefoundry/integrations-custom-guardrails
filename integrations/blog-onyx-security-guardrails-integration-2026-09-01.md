<!-- Meta description: Add Onyx Security AI Guard to the TrueFoundry AI Gateway to enforce LLM guardrails on every prompt and response, with no application code changes needed. -->
<!-- Primary KW: LLM guardrails | SV: 390 | KD: 58 (Semrush US, live) -->
<!-- Secondary KWs: AI guardrails (SV 720 / KD 41); LLM security (SV 1600 / KD 42); prompt injection detection (SV 70 / KD 42) -->
<!-- Slug: /blog/onyx-security-guardrails-integration -->
<!-- Cannibalization (Sheet3): No existing page targets "LLM guardrails" as primary KW. Nearby: /blog/enterprise-ai-security-with-mcp-gateway-runtime-guardrails (primary: enterprise ai security); live sibling integrations exist but do not own this KW. -->
<!-- Conversion intent: Conversion Potential (integration tutorial) -->

![Onyx Security x TrueFoundry AI Gateway](banner-onyx-security-guardrails-integration.png)

# Onyx Security AI Guard integration with TrueFoundry AI Gateway: policy guardrails for every LLM call

If you route production traffic through an LLM gateway, every prompt and every response is a place where something can go wrong: a prompt-injection attempt, a jailbreak, sensitive data leaving your walls. Most teams bolt on checks per application, which means the rules drift and coverage has gaps. This guide shows how to enforce one set of policies across all of it by connecting Onyx Security AI Guard to the TrueFoundry AI Gateway, so the same guardrails run on every model call without touching application code.

## Why add Onyx guardrails to your gateway

The gateway is the one point every request already passes through. Putting guardrails there instead of in each app means the policy is enforced once and applies everywhere. Through the [TrueFoundry AI Gateway](https://www.truefoundry.com/blog/llm-gateway) you already get unified access to 1,000+ LLMs behind a single OpenAI-compatible API, with roughly 3-4 ms of added latency at 350+ RPS on a single vCPU. Adding Onyx as a guardrail extends that same hot path with security checks.

Onyx Security runs an AI Guard policy engine. You define rules in the Onyx console (prompt-injection defense, jailbreak detection, data-exfiltration checks, and content policies) and the gateway calls Onyx to evaluate traffic against them. The policy logic lives in Onyx; the gateway handles enforcement. A request that violates a rule is blocked before it reaches the model, and a response can be screened before it reaches the user.

Here is how to set it up.

## Prerequisites

- A TrueFoundry account with at least one model provider configured on the AI Gateway.
- An Onyx AI Guard policy with a Guard Token from the policy's AI Guard URL (not an MCP or API Keys page token), plus Input and Output rules for what you want enforced.
- A gateway that honors `verdict: false` on HTTP 200 (current TrueFoundry custom-guardrail contract).

## Step-by-step integration guide

Onyx exposes a dedicated `/truefoundry` evaluate endpoint that speaks the gateway's custom-guardrail contract directly. No adapter or wrapper is required.

### Step 1: Build the Onyx evaluate URL

In Onyx, open **Policies → Runtime Policies → AI Guard → ⋮ → AI Guard URL**. Copy the tenant hostname and Guard Token, then form:

```
https://<routing-id>.ai-guard.onyx.security/guard/evaluate/v1/<guard-token>/truefoundry
```

Use your tenant host. The bare `https://ai-guard.onyx.security` host is not routed and returns 404.

### Step 2: Register the custom guardrail configs

In the dashboard, go to AI Gateway, then Guardrails, then New Guardrails Group. Name it `onyx-security` and add two Custom Guardrail configs that share the same URL.

| Name | Operation | Target | Auth Data | Enforcing Strategy | Config |
|---|---|---|---|---|---|
| `onyx-input` | Validate | Request | None | Enforce But Ignore On Error | `{}` |
| `onyx-output` | Validate | Response | None | Enforce But Ignore On Error | `{}` |

The Guard Token in the URL path is the auth to Onyx — leave Auth Data empty.

### Step 3: Attach the guardrail to traffic

Attach the group to a model in the model's guardrail settings, or pass it per request with a header so you can test without changing a model:

```json
{
  "llm_input_guardrails": ["onyx-security/onyx-input"],
  "llm_output_guardrails": ["onyx-security/onyx-output"]
}
```

Send a benign prompt and it passes through to the model. Send one that trips an Onyx rule and the gateway blocks it under Enforce / Enforce But Ignore On Error, returning `guardrail_checks_failed` instead of a model answer. For output checks, set `"stream": false`.

## What you unlock: centralized guardrails for every model

### Input and output validation

The input rail screens prompts before the model runs; the output rail screens responses before they reach the user. Onyx evaluates each against your policy's Input-direction and Output-direction rules.

### Policy-driven blocking

Blocks carry the message you configured in Onyx, so the caller sees your wording, not a generic error. You change enforcement by editing the policy in the Onyx console, with no redeploy of gateway code.

### One policy across every application

Because the check runs at the gateway, every team and every app that routes through it inherits the same guardrails. There is no per-application integration to maintain and no coverage gap when a new service ships.

### Observability in the same place

Guardrail decisions are traced alongside the rest of the request. The gateway is OpenTelemetry-compliant, so you can see which requests were blocked and why in the same stack you already use for latency and cost. Onyx Runtime Alerts show the matching policy hits.

## FAQ

**Q: What are LLM guardrails on an AI gateway?**
A: Guardrails are checks the gateway runs on prompts and responses before they reach the model or the user. On the TrueFoundry AI Gateway they run as configurable policies, so you can block prompt injection, jailbreaks, or data leakage centrally rather than in each application.

**Q: Does the Onyx guardrail run on both prompts and responses?**
A: Yes. Register two configs against the same `/truefoundry` URL, one on Request and one on Response. Whether a given phrase is blocked on output depends on your Onyx policy having an Output-direction rule; Input and Output are configured separately in Onyx.

**Q: Do I have to change my application code to add guardrails?**
A: No. The guardrail runs at the gateway. You attach it to a model or pass a header on the request, and the same application code keeps working.

**Q: Do I need a wrapper service?**
A: No. Point Custom Guardrails at Onyx `/truefoundry` directly. An optional local forwarder exists in the open-source repo for smoke tests only.

**Q: Can I deploy TrueFoundry in my own VPC or on-prem?**
A: Yes. TrueFoundry runs in your VPC, on-prem, air-gapped, or across clouds, and no data leaves your domain. This is the main reason regulated teams choose it over SaaS-only gateways.

**Q: Does it integrate with my existing observability stack?**
A: Yes. The gateway is OpenTelemetry-compliant and plugs into Grafana, Datadog, or Prometheus, tracing each request from prompt to model, guardrail decisions included.

## Related reading

- [What is an LLM Gateway?](https://www.truefoundry.com/blog/llm-gateway) - the architecture behind this integration
- [LiteLLM Alternatives](https://www.truefoundry.com/blog/litellm-alternatives) - how TrueFoundry compares for production teams
- [Wiring DeepKeep AI Firewall as a Custom Guardrail](https://www.truefoundry.com/blog/wiring-deepkeeps-ai-firewall-into-truefoundry-ai-gateway-as-a-custom-guardrail) - another vendor guardrail on the same custom-guardrail path

## Conclusion

Security does not have to slow your developers down. With Onyx AI Guard running as a guardrail on the TrueFoundry AI Gateway, prompt-injection and policy checks apply to every LLM call from one place, input and output, with no per-application work. Start integrating on the [TrueFoundry AI Gateway](https://www.truefoundry.com/ai-gateway).

<!-- SEO layer filled 2026-09-01 via Semrush + Sheet3. CTA confirmed: soft /ai-gateway. Updated 2026-09-21 for /truefoundry direct path. -->
