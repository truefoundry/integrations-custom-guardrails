"""Build the Bigeye access-decision request and interpret its response.

Requests are camelCase (proto3 JSON, as the OpenAPI and the Kong plugin use).
Responses are read in camelCase or snake_case, since Bigeye's docs show both.
Pure functions only, so the smoke test can reuse them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from guardrail._extract import AccessTargets
from guardrail._settings import BigeyeSettings, normalize_decision

AI_PLATFORM = "AI_PLATFORM_TRUEFOUNDRY"
USER_SUBJECT_TYPE = "user"
SQL_TYPE = "ACCESS_OBJECT_TYPE_SQL"
TABLE_TYPE = "ACCESS_OBJECT_TYPE_TABLE"
COLUMN_TYPE = "ACCESS_OBJECT_TYPE_COLUMN"
KONG_NUMERIC_SQL_TYPES = (3, "3")
SQL_ASSET_LABEL = "<sql>"
MAX_ASSET_NAME_CHARS = 200
MAX_ASSETS_IN_MESSAGE = 5
SIGNALS = (("numSensitivities", "sensitive findings"), ("numOpenIssues", "open issues"))
SIGNAL_KEYS = frozenset(key for key, _ in SIGNALS)
UNSPECIFIED = "UNSPECIFIED"
UNKNOWN_DECISION = "UNKNOWN"
# Bigeye's ranking, least to most severe. UNSPECIFIED is not ranked.
SEVERITY = ("ALLOW", "WARN", "REQUIRE_CONFIRMATION", "DENY")


class DecisionError(Exception):
    """Bigeye answered 2xx but without a usable decision. Surfaces as HTTP 502."""


@dataclass
class AssetDecision:
    name: str
    decision: str
    signals: dict = field(default_factory=dict)
    has_attributes: bool = True

    @property
    def uncataloged(self) -> bool:
        """Bigeye returns an asset missing from its catalog with no decision and no attributes."""
        return self.decision == UNSPECIFIED and not self.has_attributes


@dataclass
class ParsedDecision:
    overall: str
    assets: list
    unresolved: list = field(default_factory=list)

    @property
    def unresolved_count(self) -> int:
        return len(self.unresolved)

    @property
    def uncataloged(self) -> list:
        return [a for a in self.assets if a.uncataloged]

    @property
    def assets_found(self) -> int:
        return len(self.assets) + len(self.unresolved)


def _get(obj: dict, camel: str, snake: str) -> Any:
    return obj[camel] if camel in obj else obj.get(snake)


def _access_object(object_type: str, external_id: str, settings: BigeyeSettings) -> dict:
    obj: dict = {"type": object_type, "externalId": external_id, "workspaceId": settings.workspace_id}
    if settings.source_name:
        obj["sourceName"] = settings.source_name
    if settings.warehouse_type:
        obj["sourceType"] = settings.warehouse_type
    return obj


def build_access_objects(targets: AccessTargets, settings: BigeyeSettings) -> list:
    objects = [_access_object(TABLE_TYPE, name, settings) for name in targets.tables]
    objects += [_access_object(COLUMN_TYPE, name, settings) for name in targets.columns]
    if targets.sql:
        objects.append(_access_object(SQL_TYPE, targets.sql, settings))
    return objects


def resolve_identity(user: dict, agent_id: str) -> tuple:
    """Return ``(agentExternalId, username)``. Machine identities are the agent; users are audit-only usernames."""
    slug = user.get("subjectSlug") if isinstance(user.get("subjectSlug"), str) else None
    if user.get("subjectType") == USER_SUBJECT_TYPE:
        return agent_id, slug
    return slug or agent_id, None


def build_request_body(targets: AccessTargets, settings: BigeyeSettings, user: dict, span_id: str) -> dict:
    agent_external_id, username = resolve_identity(user or {}, settings.agent_id)
    body: dict = {
        "accessObjects": build_access_objects(targets, settings),
        "workspace": settings.workspace_id,
        "agentExternalId": agent_external_id,
        "aiAgent": {"name": agent_external_id, "platform": AI_PLATFORM},
        "operation": settings.operation,
        "spanExternalId": span_id,
        "evaluatePolicy": settings.evaluate_policy,
    }
    if username:
        body["username"] = username
    return body


def _asset_name(access_object: dict) -> str:
    """Asset name for summaries and messages. SQL text is never echoed back."""
    object_type = access_object.get("type")
    external_id = _get(access_object, "externalId", "external_id")
    if object_type == SQL_TYPE or object_type in KONG_NUMERIC_SQL_TYPES:
        return SQL_ASSET_LABEL
    if isinstance(external_id, str) and external_id.strip():
        if any(ch.isspace() for ch in external_id.strip()):
            return SQL_ASSET_LABEL
        return external_id.strip()[:MAX_ASSET_NAME_CHARS]
    catalog_id = access_object.get("id")
    return f"<id:{catalog_id}>" if catalog_id is not None else "<unnamed>"


def _signals(attributes: object) -> dict:
    signals: dict = {}
    for attribute in attributes if isinstance(attributes, list) else []:
        if not isinstance(attribute, dict) or attribute.get("key") not in SIGNAL_KEYS:
            continue
        try:
            signals[attribute["key"]] = int(str(attribute.get("value")).strip())
        except ValueError:
            continue
    return signals


def _asset_decision(raw: object) -> str:
    """proto3 JSON omits a default enum value, so a missing decision is UNSPECIFIED."""
    if raw is None:
        return UNSPECIFIED
    return normalize_decision(raw) or UNKNOWN_DECISION


def _asset(entry: object) -> AssetDecision:
    fields = entry if isinstance(entry, dict) else {}
    access_object = _get(fields, "accessObject", "access_object")
    attributes = _get(fields, "accessObjectAttributes", "access_object_attributes")
    return AssetDecision(
        name=_asset_name(access_object if isinstance(access_object, dict) else {}),
        decision=_asset_decision(_get(fields, "accessDecision", "access_decision")),
        signals=_signals(attributes),
        has_attributes=bool(_list(attributes)),
    )


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _most_severe(assets: list) -> str:
    """Bigeye's rule for a missing overall decision: the most severe asset decision, or UNSPECIFIED if none."""
    if any(a.decision == UNKNOWN_DECISION for a in assets):
        raise DecisionError("Bigeye returned an unknown access decision")
    decided = [a.decision for a in assets if a.decision != UNSPECIFIED]
    return max(decided, key=SEVERITY.index, default=UNSPECIFIED)


def parse_response(data: dict) -> ParsedDecision:
    decisions = _list(_get(data, "accessObjectDecisions", "access_object_decisions"))
    unresolved = _list(_get(data, "unresolvedAccessObjects", "unresolved_access_objects"))
    assets = [_asset(entry) for entry in decisions]
    raw_overall = _get(data, "overallDecision", "overall_decision")

    if raw_overall is None:
        overall = _most_severe(assets)
    else:
        overall = normalize_decision(raw_overall)
        if overall is None:
            raise DecisionError("Bigeye returned an unknown overall decision")

    return ParsedDecision(
        overall=overall,
        assets=assets,
        unresolved=[_asset_name(obj if isinstance(obj, dict) else {}) for obj in unresolved],
    )


def blocking_decision(parsed: ParsedDecision, block_on: frozenset) -> Optional[str]:
    """The decision that blocks the call, or None to allow.

    UNSPECIFIED in ``block_on`` also blocks when an object is unresolved or an asset isn't in
    Bigeye's catalog, whatever the overall decision. A cataloged asset without a decision
    doesn't block on its own, because Bigeye leaves per-asset decisions unset in policy mode.
    """
    if parsed.overall in block_on:
        return parsed.overall
    if UNSPECIFIED in block_on and (parsed.unresolved or parsed.uncataloged):
        return UNSPECIFIED
    return None


def _describe(asset: AssetDecision) -> str:
    present = [f"{label}: {asset.signals[key]}" for key, label in SIGNALS if key in asset.signals]
    return f"{asset.name} ({', '.join(present)})" if present else asset.name


def block_message(parsed: ParsedDecision, decision: str) -> str:
    """Short reason naming the assets behind the blocking decision, e.g.
    ``Bigeye DENY: DB.SCHEMA.TABLE.COL (sensitive findings: 1, open issues: 0)``.
    """
    if decision == UNSPECIFIED:
        culprits = [_describe(a) for a in parsed.uncataloged]
        culprits += [f"{name} (unresolved)" for name in parsed.unresolved]
    else:
        culprits = [_describe(a) for a in parsed.assets if a.decision == decision]
    if not culprits:
        return f"Bigeye {decision}"
    shown = "; ".join(culprits[:MAX_ASSETS_IN_MESSAGE])
    hidden = len(culprits) - MAX_ASSETS_IN_MESSAGE
    return f"Bigeye {decision}: {shown}" + (f"; +{hidden} more" if hidden > 0 else "")


def summary(parsed: ParsedDecision, span_id: str, latency_ms: int) -> dict:
    return {
        "checked": True,
        "span_external_id": span_id,
        "overall_decision": parsed.overall,
        "assets": [{"name": a.name, "decision": a.decision} for a in parsed.assets],
        "unresolved_count": parsed.unresolved_count,
        "uncataloged_count": len(parsed.uncataloged),
        "assets_found": parsed.assets_found,
        "latency_ms": latency_ms,
    }


def unchecked_summary(reason: str) -> dict:
    return {"checked": False, "reason": reason}
