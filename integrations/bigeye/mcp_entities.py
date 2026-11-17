"""Models for the MCP tool pre-invoke hook, kept apart so entities.py stays identical to _template.

On MCP hooks the gateway sends the raw tool arguments as ``requestBody``, the
whole custom-guardrail config object (``{url, headers, config}``) as ``config``,
and may add ``context.claims``. Metadata values are not guaranteed to be
strings, so the models accept any JSON and keep unknown fields.
"""

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from entities import ValidateGuardrailResponse


class McpRequestContext(BaseModel):
    model_config = ConfigDict(extra="allow")

    user: dict[str, Any] = Field(default_factory=dict)
    metadata: Optional[dict[str, Any]] = None
    claims: Optional[dict[str, Any]] = None


class McpPreInvokeRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    requestBody: dict[str, Any] = Field(default_factory=dict)
    config: Optional[dict[str, Any]] = None
    context: McpRequestContext = Field(default_factory=McpRequestContext)


class BigeyeValidateResponse(ValidateGuardrailResponse):
    """Validate response plus a ``bigeye`` summary that lands on the gateway trace span."""

    bigeye: dict[str, Any]
