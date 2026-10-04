"""Deterministic demo fallback: the loop without an LLM.

Plays the agent's side through the *real* MCP tools, in-process, in the order
a good agent would call them, then hands off to the interactive
`ctl approve` so the human step stays exactly what it is live. Use it when
conference Wi-Fi or model variance lets you down, or record it
(`asciinema rec -c "make replay"`) as the backup video.

    python3 -m scenarios.replay            # pause for Enter between steps
    python3 -m scenarios.replay --no-pause
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from typing import Any

import mcp_server.server as srv
from control_plane.fleet import get_fleet
from control_plane.store import Store

TRANSFORM_PATCH = {
    "processors": {"transform/strip-sku": {"metric_statements": [{
        "context": "datapoint",
        "statements": ['delete_key(attributes, "sku_id") where '
                       'resource.attributes["service.name"] == "checkout"'],
    }]}},
    "service": {"pipelines": {"metrics": {"processors": ["batch", "transform/strip-sku"]}}},
}


def _call(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    content, _ = asyncio.run(srv.mcp.call_tool(tool, args))
    return json.loads(content[0].text)


def _say(title: str, detail: Any, pause: bool) -> None:
    print(f"\n\033[1;33m▶ {title}\033[0m")
    if detail is not None:
        text = detail if isinstance(detail, str) else json.dumps(detail, indent=2)
        print(text[:1500])
    if pause:
        input("\033[2m  [enter]\033[0m")


def agent_side(pause: bool = False) -> str:
    """Run the agent's half of the loop; return the pending proposal id."""
    _say("fleet_overview", _call("fleet_overview", {}), pause)
    m = _call("query_metrics", {"window_minutes": 30, "step_minutes": 10})
    first, last = m["points"][0]["by_service"], m["points"][-1]["by_service"]
    _say("query_metrics: 30m, per service",
         {s: f"{first[s]} → {last[s]}" for s in first}, pause)
    logs = _call("query_logs", {"service": "checkout", "limit": 6})
    _say("query_logs: checkout", [l["message"] for l in logs["lines"]], pause)
    _say("get_guardrails", _call("get_guardrails", {}), pause)

    evidence = [
        {"receipt_id": m["evidence_receipt"],
         "observation": f"checkout active series {first['checkout']} → {last['checkout']}"},
        {"receipt_id": logs["evidence_receipt"],
         "observation": "WARN: high-cardinality label 'sku_id' on checkout metrics"},
    ]
    reason = "checkout active series grew ~8x from the sku_id label; strip it at the collector"
    first_try = _call("propose_config_change", {
        "reason": reason, "selector": {"labels": {"service": "checkout"}},
        "config_patch": TRANSFORM_PATCH, "evidence": evidence})
    _say("propose_config_change (selector: service=checkout)",
         {k: first_try[k] for k in ("status", "policy_verdict")}, pause)
    second = _call("propose_config_change", {
        "reason": reason,
        "selector": {"labels": {"service": "checkout", "env": "prod", "tier": "standard"}},
        "config_patch": TRANSFORM_PATCH, "evidence": evidence})
    _say("propose_config_change (narrowed to tier=standard)",
         {k: second[k] for k in ("proposal_id", "status", "matched_agents", "next_step")}, pause)
    return second["proposal_id"]


def main() -> None:
    ap = argparse.ArgumentParser(prog="replay")
    ap.add_argument("--no-pause", action="store_true")
    args = ap.parse_args()
    pause = not args.no_pause

    store = Store()
    store.reset()
    fleet = get_fleet(store)
    fleet.reset()
    fleet.set_scenario({"active": "cardinality_explosion", "service": "checkout",
                        "label": "sku_id", "multiplier": 8.0})
    pid = agent_side(pause)

    _say("Human: ctl approve (interactive, as it is live)", None, False)
    subprocess.run([sys.executable, "-m", "cli.ctl", "approve", pid])
    _say("Human: ctl audit", None, False)
    subprocess.run([sys.executable, "-m", "cli.ctl", "audit", "--limit", "15"])


if __name__ == "__main__":
    main()
