"""Pull SQL, table names and column names out of MCP tool arguments.

Only top-level arguments are read, under the names in the config's ``sql_keys``,
``table_keys`` and ``column_keys``. The first non-empty SQL key wins. Name values
may be a list, a JSON-array string or a comma-separated string. Names pass through
unchanged; Bigeye resolves them and reports partial names as unresolved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class AccessTargets:
    sql: Optional[str]
    tables: tuple
    columns: tuple

    @property
    def empty(self) -> bool:
        return not (self.sql or self.tables or self.columns)


def _first_sql(arguments: dict, keys: tuple) -> Optional[str]:
    for key in keys:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _split_names(value: object) -> list:
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


def extract_targets(arguments: dict, sql_keys: tuple, table_keys: tuple, column_keys: tuple) -> AccessTargets:
    return AccessTargets(
        sql=_first_sql(arguments, sql_keys),
        tables=_names(arguments, table_keys),
        columns=_names(arguments, column_keys),
    )
