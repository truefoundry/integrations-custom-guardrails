"""Test-only SQL MCP server for the Bigeye end-to-end test. Returns canned rows; never touches a database.

Every tool invocation is logged with a timestamp, so a blocked call is visible as a missing log line.
Streamable HTTP at /mcp, health check at /health.
"""

import logging
import os

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stub-sql-mcp")

MAX_LOGGED_QUERY_CHARS = 300
CANNED_COLUMNS = ["ORDER_ID", "REGION", "ORDER_TOTAL"]
CANNED_ROWS = [[1001, "EMEA", 120.5], [1002, "AMER", 89.0], [1003, "APAC", 240.25]]

mcp = FastMCP("stub-sql-mcp", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), stateless_http=True)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@mcp.tool()
def run_query(query: str) -> dict:
    """Run a read-only SQL query against the analytics warehouse. Use fully qualified DATABASE.SCHEMA.TABLE names."""
    log.info("INVOKED run_query query=%r", query[:MAX_LOGGED_QUERY_CHARS])
    return {"columns": CANNED_COLUMNS, "rows": CANNED_ROWS, "note": "canned rows from stub-sql-mcp"}


@mcp.tool()
def describe_table(table: str) -> dict:
    """Describe a table's columns. Pass the fully qualified name DATABASE.SCHEMA.TABLE."""
    log.info("INVOKED describe_table table=%r", table)
    return {"table": table, "columns": [{"name": c, "type": "VARCHAR"} for c in CANNED_COLUMNS]}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
