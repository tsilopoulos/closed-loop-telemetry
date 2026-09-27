"""Rollout engine: canary -> verify -> promote, with auto-rollback.

Only `cli/ctl.py approve` calls `start_rollout`. The MCP server has no code
path into this module's mutating functions — that is checked by a test
(tests/test_loop.py::test_mcp_server_cannot_apply).

The reference implementation runs the stages synchronously in "sim time".
The production version of this engine is asynchronous (bake periods between
stages); see CLAUDE.md roadmap.
"""

from __future__ import annotations

from typing import Any

from .models import ConfigProposal, Rollout, new_id, now
from .policy import canary_size, load_policy, protected_targets
from .store import Store


class RolloutError(Exception):
    pass


def start_rollout(store: Store, fleet, proposal_id: str, approver: str) -> Rollout:
    raw = store.get_proposal(proposal_id)
    if raw is None:
        raise RolloutError(f"No such proposal: {proposal_id}")
    proposal = ConfigProposal.from_dict(raw)

    if proposal.status != "pending_approval":
        raise RolloutError(
            f"Proposal {proposal_id} is '{proposal.status}', not pending_approval."
        )
    if not (proposal.policy_verdict or {}).get("allowed"):
        raise RolloutError("Proposal failed policy validation and can never be applied.")
    if approver.startswith("ai-agent"):
        raise RolloutError("Approval requires a human actor. This is the point.")

    policy = load_policy()
    matched = fleet.select(proposal.selector)
    if not matched:
        raise RolloutError("Selector matches no agents.")
    # Labels can change between proposal and approval; re-check the live set.
    if hit := protected_targets(matched, policy):
        raise RolloutError(f"Selector now matches protected agents {hit}; refusing.")

    matched_ids = [a.agent_id for a in matched]
    canaries = matched_ids[: canary_size(len(matched_ids), policy)]

    rollout = Rollout(
        rollout_id=new_id("roll"),
        proposal_id=proposal.proposal_id,
        created_at=now(),
        matched_agent_ids=matched_ids,
        canary_agent_ids=canaries,
    )
    proposal.status = "canarying"
    proposal.decided_by = approver
    proposal.decided_at = now()
    proposal.rollout_id = rollout.rollout_id
    store.put_proposal(proposal.to_dict())
    store.put_rollout(rollout.to_dict())
    store.audit(approver, "rollout.start", {
        "proposal_id": proposal.proposal_id,
        "rollout_id": rollout.rollout_id,
        "matched": len(matched_ids),
        "canaries": canaries,
    })

    # --- Stage 1: canary ----------------------------------------------------
    before_canary_series = fleet.series_for_agents(canaries)
    fleet.apply_patch(canaries, proposal.config_patch)
    store.audit("rollout-engine", "rollout.canary_applied", {
        "rollout_id": rollout.rollout_id, "agents": canaries,
    })

    # --- Stage 2: verify ----------------------------------------------------
    rollout.status = "verifying"
    verification: dict[str, Any] = {}
    ver_policy = policy.get("verification", {})

    canary_agents = [fleet.get_agent(aid) for aid in canaries]
    all_healthy = all(a.healthy for a in canary_agents)
    verification["all_canaries_healthy"] = all_healthy

    after_canary_series = fleet.series_for_agents(canaries)
    verification["canary_series_before"] = before_canary_series
    verification["canary_series_after"] = after_canary_series
    series_ok = after_canary_series <= before_canary_series
    verification["no_series_increase"] = series_ok

    # Two-sided gate: fewer series is not automatically better. A filter that
    # drops everything "wins" the gate above; this one catches it.
    min_frac = float(ver_policy.get("min_series_vs_baseline", 0.5))
    baseline = fleet.baseline_series_for_agents(canaries)
    per_service = fleet.service_series_for_agents(canaries)
    starved = sorted(s for s, b in baseline.items() if b and per_service[s] < min_frac * b)
    verification["services_below_baseline"] = starved
    verification["signal_preserved"] = not starved

    failed = (
        (ver_policy.get("require_all_canaries_healthy", True) and not all_healthy)
        or (ver_policy.get("require_no_series_increase", True) and not series_ok)
        or bool(starved)
    )
    rollout.verification = verification

    if failed:
        # --- Auto-rollback ----------------------------------------------------
        fleet.rollback(canaries)
        rollout.status = "rolled_back"
        rollout.finished_at = now()
        proposal.status = "rolled_back"
        store.put_rollout(rollout.to_dict())
        store.put_proposal(proposal.to_dict())
        store.audit("rollout-engine", "rollout.rolled_back", {
            "rollout_id": rollout.rollout_id, "verification": verification,
        })
        return rollout

    # --- Stage 3: promote -----------------------------------------------------
    rollout.status = "promoting"
    remaining = [aid for aid in matched_ids if aid not in set(canaries)]
    if remaining:
        fleet.apply_patch(remaining, proposal.config_patch)
    rollout.status = "applied"
    rollout.finished_at = now()
    proposal.status = "applied"
    store.put_rollout(rollout.to_dict())
    store.put_proposal(proposal.to_dict())
    store.audit("rollout-engine", "rollout.applied", {
        "rollout_id": rollout.rollout_id,
        "total_agents": len(matched_ids),
        "verification": verification,
    })
    return rollout


def manual_rollback(store: Store, fleet, rollout_id: str, actor: str) -> Rollout:
    raw = store.get_rollout(rollout_id)
    if raw is None:
        raise RolloutError(f"No such rollout: {rollout_id}")
    rollout = Rollout.from_dict(raw)
    fleet.rollback(rollout.matched_agent_ids)
    rollout.status = "rolled_back"
    rollout.finished_at = now()
    store.put_rollout(rollout.to_dict())
    p = store.get_proposal(rollout.proposal_id)
    if p:
        p["status"] = "rolled_back"
        store.put_proposal(p)
    store.audit(actor, "rollout.manual_rollback", {"rollout_id": rollout_id})
    return rollout
