"""OpAMP bridge (integration stub).

This adapter is the seam where the reference implementation meets a real
OpAMP deployment. It intentionally mirrors `SimulatedFleet`'s interface so the
policy engine, rollout engine, and MCP server don't change at all when you
swap `CTL_FLEET=opamp`.

In a real deployment this class would talk to your OpAMP server's management
API (e.g., the REST/gRPC surface of your control plane in front of opamp-go):

  - agents()/select()   -> fleet inventory from the OpAMP server's agent registry
  - apply_patch()       -> stage a new effective config and push
                           ServerToAgent.remote_config to the selected agents
  - rollback()          -> re-push the previously effective config hash
  - series_by_service() -> PromQL against your metrics backend (see backends/)
  - recent_logs()       -> LogQL/SQL against your logs backend (see backends/)

Deliberately NOT implemented here: nothing in this repo speaks the OpAMP wire
protocol directly. Keeping the bridge behind a management API preserves the
guardrail chain — the OpAMP server remains the single component with write
access to agents, and it can enforce its own authz on top of ours.
"""

from __future__ import annotations

from typing import Any

from .store import Store


class OpAMPBridge:
    def __init__(self, store: Store):
        self.store = store
        raise NotImplementedError(
            "OpAMPBridge is an integration stub. Point it at your OpAMP "
            "server's management API and implement the FleetController "
            "methods (agents, select, apply_patch, rollback, "
            "series_by_service, series_for_agents, recent_logs, scenario). "
            "See docs/architecture.md#opamp-bridge."
        )

    # The method signatures below document the contract.

    def agents(self) -> list[Any]: ...

    def select(self, selector: dict[str, Any]) -> list[Any]: ...

    def apply_patch(self, agent_ids: list[str], patch: dict[str, Any]) -> None: ...

    def rollback(self, agent_ids: list[str]) -> None: ...

    def series_by_service(self) -> dict[str, int]: ...

    def series_for_agents(self, agent_ids: list[str]) -> int: ...

    def recent_logs(self, service: str | None, limit: int) -> list[dict[str, Any]]: ...
