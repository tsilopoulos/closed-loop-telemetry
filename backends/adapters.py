"""Real telemetry backend adapters (stubs).

In the simulated build, telemetry queries are answered by the fleet model.
In a real deployment, the MCP server's query tools should proxy to your
actual backends through these adapters. They are deliberately thin HTTP
clients — the MCP tool schemas stay identical either way, which is what makes
the talk's architecture portable.

Backend-agnostic by design: anything Prometheus-compatible for metrics
(remote-read/PromQL HTTP API), anything exposing a trace search API, and any
SQL/HTTP-queryable log store will slot in here.

Wire them in by implementing `series_by_service`, `series_for_agents`, and
`recent_logs` on your `OpAMPBridge` (control_plane/opamp_bridge.py) using
these clients.
"""

from __future__ import annotations

from typing import Any


class PromCompatibleMetrics:
    """PromQL over the standard HTTP API (/api/v1/query)."""

    def __init__(self, base_url: str, tenant: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.tenant = tenant

    def instant_query(self, promql: str) -> dict[str, Any]:
        raise NotImplementedError(
            "GET {base}/api/v1/query?query=<promql> with your auth headers. "
            "Example query for the talk's demo: "
            "count by (service_name) ({__name__=~'.+'})"
        )


class TraceSearch:
    """Trace search over an HTTP API (/api/search)."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def search(self, query: str, limit: int = 20) -> dict[str, Any]:
        raise NotImplementedError("GET {base}/api/search?q=<query>&limit=<n>")


class SQLLogs:
    """Log queries over a SQL/HTTP interface."""

    def __init__(self, base_url: str, database: str = "otel"):
        self.base_url = base_url.rstrip("/")
        self.database = database

    def query(self, sql: str) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "POST <sql> to {base}/?database=<db>&default_format=JSONEachRow"
        )
