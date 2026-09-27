"""Tests for the closed loop and its guardrails.

The most important tests here are the guardrail ones: they pin down the
architectural invariants the talk is about. If someone adds an `apply` tool
to the MCP server, test_mcp_server_cannot_apply fails the build.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

# Route the store at a temp DB before importing modules that build one.
_tmp = tempfile.mkdtemp()
os.environ["CTL_DB_PATH"] = os.path.join(_tmp, "test.db")

from control_plane.fleet import SimulatedFleet  # noqa: E402
from control_plane.models import ConfigProposal  # noqa: E402
from control_plane.policy import validate_proposal  # noqa: E402
from control_plane.rollout import RolloutError, start_rollout  # noqa: E402
from control_plane.store import Store  # noqa: E402

MITIGATION_PATCH = {
    "processors": {
        "filter/drop-sku-checkout": {
            "metrics": {"datapoint": [
                'attributes["sku_id"] != nil and '
                'resource.attributes["service.name"] == "checkout"'
            ]}
        }
    },
    "service": {"pipelines": {"metrics": {
        "processors": ["batch", "filter/drop-sku-checkout"]}}},
}


# Excludes tier=payment-critical agents, as the guardrails require.
DEMO_SELECTOR = {"labels": {"service": "checkout", "env": "prod", "tier": "standard"}}


@pytest.fixture()
def env():
    store = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    fleet = SimulatedFleet(store, size=40)
    return store, fleet


def _make_proposal(env, *, patch=None, evidence=None, selector=None):
    store, fleet = env
    patch = patch or MITIGATION_PATCH
    evidence = evidence if evidence is not None else [
        "query_metrics: checkout active_series 8x baseline",
        "query_logs: WARN high-cardinality label 'sku_id'",
    ]
    selector = selector or DEMO_SELECTOR
    verdict = validate_proposal(
        config_patch=patch, selector=selector, targets=fleet.select(selector),
        evidence=evidence, reason="Mitigate checkout sku_id cardinality explosion",
    )
    p = ConfigProposal.create(
        author="ai-agent:test", reason="Mitigate checkout sku_id cardinality explosion",
        selector=selector, config_patch=patch, evidence=evidence,
    )
    p.policy_verdict = verdict.to_dict()
    if not verdict.allowed:
        p.status = "policy_rejected"
    store.put_proposal(p.to_dict())
    return p, verdict


# ---------------------------------------------------------------------------
# Policy guardrails


def test_policy_rejects_exporter_changes(env):
    store, fleet = env
    patch = {"exporters": {"otlphttp": {"endpoint": "https://evil.example.com"}}}
    _, verdict = _make_proposal(env, patch=patch)
    assert not verdict.allowed
    assert any("forbidden" in r for r in verdict.reasons)


def test_policy_requires_evidence(env):
    store, fleet = env
    _, verdict = _make_proposal(env, evidence=[])
    assert not verdict.allowed


def test_policy_protects_critical_agents(env):
    _, verdict = _make_proposal(
        env, selector={"labels": {"tier": "payment-critical"}})
    assert not verdict.allowed


@pytest.mark.parametrize("selector", [
    {"labels": {"service": "checkout"}},
    {"labels": {"service": "checkout", "env": "prod"}},
    {"all": True},
    {"agent_ids": ["otelcol-0017"]},
])
def test_policy_protects_critical_agents_matched_indirectly(env, selector):
    """Selectors that never mention `tier` still can't reach protected agents."""
    _, verdict = _make_proposal(env, selector=selector)
    assert not verdict.allowed
    assert any("protected" in r for r in verdict.reasons)


def test_policy_rejects_selector_matching_nothing(env):
    _, verdict = _make_proposal(env, selector={"labels": {"env": "nope"}})
    assert not verdict.allowed


def test_rollout_rechecks_protected_agents(env):
    """An agent relabelled protected after proposal time is still refused."""
    store, fleet = env
    p, verdict = _make_proposal(env, selector={"agent_ids": ["otelcol-0001"]})
    assert verdict.allowed
    raw = store.get_kv("fleet")
    raw["otelcol-0001"]["labels"]["tier"] = "payment-critical"
    store.put_kv("fleet", raw)
    with pytest.raises(RolloutError, match="protected"):
        start_rollout(store, fleet, p.proposal_id, approver="human:tester")


def test_ai_cannot_approve(env):
    store, fleet = env
    p, _ = _make_proposal(env)
    with pytest.raises(RolloutError, match="human"):
        start_rollout(store, fleet, p.proposal_id, approver="ai-agent:mcp")


def test_policy_rejected_proposal_can_never_roll_out(env):
    store, fleet = env
    patch = {"exporters": {"otlphttp": {"endpoint": "https://evil.example.com"}}}
    p, _ = _make_proposal(env, patch=patch)
    with pytest.raises(RolloutError):
        start_rollout(store, fleet, p.proposal_id, approver="human:tester")


def test_mcp_server_cannot_apply():
    """The MCP surface must contain no approve/apply/rollout tool."""
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "mcp_server", "server.py")).read()
    assert "start_rollout" not in src
    assert "apply_patch" not in src
    # exactly one write-shaped tool
    assert src.count("def propose_config_change") == 1


def test_mcp_server_imports():
    """A fresh install must yield a server that starts (catches SDK breaks)."""
    import mcp_server.server  # noqa: F401


# ---------------------------------------------------------------------------
# The closed loop


def test_happy_path_canary_then_promote(env):
    store, fleet = env
    fleet.set_scenario({"active": "cardinality_explosion",
                        "service": "checkout", "label": "sku_id", "multiplier": 8.0})
    before = fleet.series_by_service()["checkout"]

    p, verdict = _make_proposal(env)
    assert verdict.allowed

    rollout = start_rollout(store, fleet, p.proposal_id, approver="human:tester")
    assert rollout.status == "applied"
    assert rollout.verification["all_canaries_healthy"] is True
    assert rollout.verification["no_series_increase"] is True
    assert not any(fleet.get_agent(a).labels["tier"] == "payment-critical"
                   for a in rollout.matched_agent_ids)
    # canary cap respected (5% of matched, min 1)
    assert len(rollout.canary_agent_ids) <= max(1, len(rollout.matched_agent_ids) // 20 + 1)

    after = fleet.series_by_service()["checkout"]
    assert after < before, "mitigation should reduce exploded series"

    final = store.get_proposal(p.proposal_id)
    assert final["status"] == "applied"


def test_bad_config_auto_rolls_back(env):
    store, fleet = env
    patch = {
        "processors": {"filter/broken": {"__break__": True, "mentions": "checkout"}},
    }
    p, verdict = _make_proposal(env, patch=patch)
    assert verdict.allowed  # policy can't know it's broken; verification catches it

    rollout = start_rollout(store, fleet, p.proposal_id, approver="human:tester")
    assert rollout.status == "rolled_back"
    assert rollout.verification["all_canaries_healthy"] is False

    # canaries restored and healthy again
    for aid in rollout.canary_agent_ids:
        a = fleet.get_agent(aid)
        assert a.healthy
        assert "filter/broken" not in a.config.get("processors", {})

    final = store.get_proposal(p.proposal_id)
    assert final["status"] == "rolled_back"


def test_audit_trail_records_everything(env):
    store, fleet = env
    p, _ = _make_proposal(env)
    store.audit("ai-agent:test", "proposal.created", {"proposal_id": p.proposal_id})
    start_rollout(store, fleet, p.proposal_id, approver="human:tester")
    actions = [e["action"] for e in store.audit_log(50)]
    for expected in ("rollout.start", "rollout.canary_applied", "rollout.applied"):
        assert expected in actions
