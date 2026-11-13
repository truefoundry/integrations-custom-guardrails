# Design notes — `neuraltrust`

## Scope (v1)

LLM Input and LLM Output, Validate and Mutate. MCP Tool Pre/Post-Invoke hooks are out of scope until the custom-guardrail payload for those hooks is documented by TrueFoundry.

## Request translation

| Gateway field | TrustGuard field |
|---|---|
| `requestBody.messages` minus `system`/`developer` (input) | `payload.messages`, `direction: input`, `protocol: llm` |
| `responseBody.choices[].message` (output) | `payload.messages` as `assistant` messages, `direction: output` |
| `context.user.subjectSlug` (fallback `subjectId`) | `consumer_id` |
| `context.metadata.session_id` (if present) | `session_id` |
| `requestBody.model` | `attributes.model.{name,provider}` |
| `context.user.subjectType` | `attributes.consumer.type` |
| `context.user.subjectId` / slug if it is an email | `attributes.user.{id,email}` |
| constant | `attributes.source.application = "truefoundry-ai-gateway"` |

Only documented top-level fields are sent (`payload`, `direction`, `protocol`, `session_id`, `consumer_id`, `attributes`): TrustGuard rejects unknown top-level fields with `400`.

Completions are sent as `assistant` messages, never as `{"input": …}`: TrustGuard reads `input` as the user's message, and Toxicity / Custom Moderation only read assistant text on Output.

## Transform write-back

On Mutate routes, `transformed_payload.messages` is written back by position onto the messages that were sent (input: original indices in `requestBody.messages`; output: `choices[i].message`). `content` and `tool_calls` are replaced; every other field of the body is preserved and the **full** body is returned as `result`, as the contract requires. If the transformed message count does not match, the rail fails closed with `verdict: false`.

## Verdict mapping and fail mode

See README. Summary: `allow`/`skip`/`report` → allow; `block` → deny; `ask` → deny (configurable); `transform` → rewrite on Mutate, allow on Validate (configurable). Errors and timeouts → `502`, delegated to the TrueFoundry Enforcing Strategy. Same choices as NeuralTrust's LiteLLM integration (`ask` treated as block, transform fails closed when it cannot be applied).

## Timeout

Default 3.5 s, env `NEURALTRUST_TIMEOUT_SECONDS` or per-config `timeout_seconds`, capped at 4.5 s so the wrapper's 502 reaches the gateway before the gateway's own ~5 s guardrail timeout.
