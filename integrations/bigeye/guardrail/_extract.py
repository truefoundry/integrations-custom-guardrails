"""Pull SQL, table names and column names out of MCP tool arguments.

Keys follow Bigeye's Kong plugin: ``query``/``sql``, ``tables``/``table`` and
``columns``/``column``. Name values may be a list, a JSON-array string or a
comma-separated string. Names pass through unchanged; Bigeye resolves them and
reports partial names as unresolved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

SQL_KEYS = ("query", "sql")
TABLE_KEYS = ("tables", "table")
COLUMN_KEYS = ("columns", "column")


@dataclass(frozen=True)
class AccessTargets:
    sql: Optional[str]
    tables: tuple
    columns: tuple

    @property
    def empty(self) -> bool:
        return not (self.sql or self.tables or self.columns)


def _first_sql(arguments: dict) -> Optional[str]:
    for key in SQL_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _split_names(value: Any) -> list:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except ValueError:
                value = text.strip("[]").split(",")
        else:
            value = text.split(",")
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _names(arguments: dict, keys: tuple) -> tuple:
    names: list = []
    for key in keys:
        names.extend(_split_names(arguments.get(key)))
    return tuple(dict.fromkeys(names))


def extract_targets(arguments: dict) -> AccessTargets:
    return AccessTargets(
        sql=_first_sql(arguments),
        tables=_names(arguments, TABLE_KEYS),
        columns=_names(arguments, COLUMN_KEYS),
    )
