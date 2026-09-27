"""Fleet MCP server (stdio).

This is the *entire* surface an AI agent gets. Design rules:

1. Read tools are broad: fleet inventory, configs, metrics, logs, scenario.
2. There is exactly one write-shaped tool: `propose_config_change`. It
   creates a proposal for human review. It never touches an agent.
3. There is NO tool that approves or applies anything. Approval lives in
   `cli/ctl.py`, run by a human. Keeping that tool out of this file is a
   guardrail enforced by tests, not by prompt engineering.

Run directly:        python3 -m mcp_server.server
Via Claude Code:     .mcp.json in the repo root registers this server.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from control_plane.fleet import get_fleet
from control_plane.models import ConfigProposal
from control_plane.policy import validate_proposal
from control_plane.store import Store

mcp = FastMCP(
    "otel-fleet",
    instructions=(
        "Tools for observing an OpenTelemetry collector fleet and proposing "
        "configuration changes. You can read anything; you can apply nothing. "
        "Config changes you propose require human approval via the ctl CLI, "
        "and are validated against policy/guardrails.yaml. Always gather "
        "telemetry evidence before proposing."
    ),
)

_store = Store()
_fleet = get_fleet(_store)
AGENT_ACTOR = "ai-agent:mcp"


def _j(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)


# ---------------------------------------------------------------------------
# Read tools


@mcp.tool()
def fleet_overview() -> str:
    """Summarize the collector fleet: sizes by env/region, health, and any
    active scenario. Start here."""
    agents = _fleet.agents()
    by_env: dict[str, int] = {}
    unhealthy = []
    for a in agents:
        by_env[a.labels.get("env", "?")] = by_env.get(a.labels.get("env", "?"), 0) + 1
        if not a.healthy:
            unhealthy.append(a.agent_id)
    return _j({
        "total_agents": len(agents),
        "by_env": by_env,
        "unhealthy_agents": unhealthy,
        "active_scenario": _fleet.scenario(),
        "note": "Use query_metrics to inspect series counts per service.",
    })


@mcp.tool()
def fleet_list_agents(env: str | None = None, region: str | None = None,
                      service: str | None = None, limit: int = 20) -> str:
    """List agents, optionally filtered by env, region, or hosted service."""
    labels: dict[str, str] = {}
    if env:
        labels["env"] = env
    if region:
        labels["region"] = region
    if service:
        labels["service"] = service
    agents = _fleet.select({"labels": labels} if labels else {"all": True})
    return _j([
        {"agent_id": a.agent_id, "labels": a.labels,
         "healthy": a.healthy, "config_version": a.config_version}
        for a in agents[:limit]
    ] + ([{"truncated": len(agents) - limit}] if len(agents) > limit else []))


@mcp.tool()
def fleet_get_config(agent_id: str) -> str:
    """Fetch one agent's full effective collector configuration."""
    a = _fleet.get_agent(agent_id)
    if a is None:
        return _j({"error": f"No agent '{agent_id}'. Use fleet_list_agents first."})
    return _j({"agent_id": a.agent_id, "config_version": a.config_version,
               "healthy": a.healthy, "config": a.config})


@mcp.tool()
def query_metrics(metric: str = "active_series", group_by: str = "service") -> str:
    """Query fleet telemetry metrics.

    Supported: metric='active_series', group_by='service' — total active
    metric series per service across the fleet. A sudden multiple-x jump for
    one service indicates a cardinality explosion.
    (Real deployment: this proxies PromQL to the metrics backend; see backends/.)
    """
    if metric != "active_series" or group_by != "service":
        return _j({"error": "This reference build supports metric='active_series', "
                            "group_by='service' only."})
    return _j({"metric": metric, "by_service": _fleet.series_by_service(),
               "baseline_per_agent": "checkout=1200 inventory=900 fulfillment=800 "
                                     "order-mgmt=1000 search=700 (multiply by hosting agents)"})


@mcp.tool()
def query_logs(service: str | None = None, limit: int = 20) -> str:
    """Fetch recent log lines, optionally for one service. WARN/ERROR lines
    often name the label or upstream causing trouble — cite them as evidence.
    (Real deployment: this proxies to the logs backend; see backends/.)"""
    return _j(_fleet.recent_logs(service=service, limit=limit))


@mcp.tool()
def get_guardrails() -> str:
    """Show the policy contract governing what you may propose: allowed and
    forbidden config paths, canary limits, and evidence requirements. Read
    this before drafting a config patch."""
    from control_plane.policy import load_policy
    return _j(load_policy())


@mcp.tool()
def list_proposals(status: str | None = None) -> str:
    """List config proposals (optionally by status: pending_approval,
    applied, rolled_back, rejected, policy_rejected)."""
    return _j([
        {k: p[k] for k in ("proposal_id", "status", "author", "reason", "created_at")}
        for p in _store.list_proposals(status)
    ])


@mcp.tool()
def get_proposal(proposal_id: str) -> str:
    """Fetch a proposal in full, including its policy verdict and (if
    approved) rollout status and verification results."""
    p = _store.get_proposal(proposal_id)
    if p is None:
        return _j({"error": f"No proposal '{proposal_id}'."})
    out = dict(p)
    if p.get("rollout_id"):
        out["rollout"] = _store.get_rollout(p["rollout_id"])
    return _j(out)


# ---------------------------------------------------------------------------
# The single write-shaped tool


@mcp.tool()
def propose_config_change(reason: str, selector_json: str, config_patch_json: str,
                          evidence_json: str) -> str:
    """Propose a fleet configuration change for HUMAN review. Nothing is
    applied by this tool.

    Args:
        reason: Why this change is needed (min 20 chars). Reference what you
            observed, e.g. "checkout active series grew 8x due to sku_id label".
        selector_json: JSON selector for target agents. One of:
            {"labels": {"env": "prod"}} | {"labels": {"service": "checkout"}} |
            {"agent_ids": ["otelcol-0001"]} | {"all": true}
            Labels are ANDed. The matched set must not include agents with a
            protected label (see get_guardrails) — add e.g. "tier": "standard".
        config_patch_json: JSON merge patch onto agent config. Only telemetry-
            shaping processors are allowed (see get_guardrails). Example:
            {"processors": {"filter/drop-sku": {"metrics": {"datapoint":
            ["attributes[\\"sku_id\\"] != nil and resource.attributes[\\"service.name\\"] == \\"checkout\\""]}}},
             "service": {"pipelines": {"metrics": {"processors": ["batch", "filter/drop-sku"]}}}}
        evidence_json: JSON list of strings — the queries/observations that
            justify the change.

    Returns the proposal with its policy verdict. If policy rejects it, fix
    the patch and propose again; do not attempt to widen your access.
    """
    try:
        selector = json.loads(selector_json)
        config_patch = json.loads(config_patch_json)
        evidence = json.loads(evidence_json)
        assert isinstance(evidence, list)
    except Exception as e:  # noqa: BLE001
        return _j({"error": f"Malformed JSON argument: {e}"})

    targets = _fleet.select(selector)
    verdict = validate_proposal(
        config_patch=config_patch, selector=selector, targets=targets,
        evidence=evidence, reason=reason,
    )
    proposal = ConfigProposal.create(
        author=AGENT_ACTOR, reason=reason, selector=selector,
        config_patch=config_patch, evidence=evidence,
    )
    proposal.policy_verdict = verdict.to_dict()
    if not verdict.allowed:
        proposal.status = "policy_rejected"
    _store.put_proposal(proposal.to_dict())
    _store.audit(AGENT_ACTOR, "proposal.created", {
        "proposal_id": proposal.proposal_id,
        "allowed": verdict.allowed,
        "touched_paths": verdict.touched_paths,
    })

    matched = len(targets) if verdict.allowed else 0
    return _j({
        "proposal_id": proposal.proposal_id,
        "status": proposal.status,
        "policy_verdict": proposal.policy_verdict,
        "matched_agents": matched,
        "next_step": (
            "Awaiting human review: run `python3 -m cli.ctl list` then "
            f"`python3 -m cli.ctl approve {proposal.proposal_id}`."
            if verdict.allowed else
            "Rejected by policy. Read the reasons, adjust, and re-propose."
        ),
    })


if __name__ == "__main__":
    mcp.run()
