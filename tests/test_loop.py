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


TRANSFORM_PATCH = {
    "processors": {
        "transform/strip-sku": {
            "metric_statements": [{
                "context": "datapoint",
                "statements": [
                    'delete_key(attributes, "sku_id") where '
                    'resource.attributes["service.name"] == "checkout"'
                ],
            }]
        }
    },
    "service": {"pipelines": {"metrics": {
        "processors": ["batch", "transform/strip-sku"]}}},
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
    selector = DEMO_SELECTOR if selector is None else selector
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


@pytest.mark.parametrize("patch", [
    {"service": {"pipelines": {"metrics": {"exporters": []}}}},
    {"service": {"pipelines": {"metrics": {"receivers": []}}}},
    {"service": {"pipelines": {"metrics": None}}},
    {"service": {"pipelines": None}},
    {"service": None},
])
def test_policy_rejects_pipeline_rewiring(env, patch):
    """Only pipeline *processor lists* are proposable; blackholing is not."""
    _, verdict = _make_proposal(env, patch=patch)
    assert not verdict.allowed


def test_policy_allows_pipeline_processor_lists(env):
    _, verdict = _make_proposal(env, patch={
        "service": {"pipelines": {"logs": {"processors": ["batch"]}}}})
    assert verdict.allowed


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


@pytest.mark.parametrize("approver", ["claude", "Ai-Agent:x", "", "human:", "agent:human:x"])
def test_only_human_prefixed_approvers(env, approver):
    """Approval is an allowlist on `human:<name>`, not a denylist on ai-agent."""
    store, fleet = env
    p, _ = _make_proposal(env)
    with pytest.raises(RolloutError, match="human"):
        start_rollout(store, fleet, p.proposal_id, approver=approver)


def test_policy_change_invalidates_pending_verdicts(env, tmp_path, monkeypatch):
    """A verdict is honoured only under the policy that issued it."""
    import shutil
    from control_plane import policy as policy_mod
    store, fleet = env
    pol = tmp_path / "guardrails.yaml"
    shutil.copy(policy_mod.DEFAULT_POLICY_PATH, pol)
    monkeypatch.setattr(policy_mod, "DEFAULT_POLICY_PATH", str(pol))
    p, verdict = _make_proposal(env)
    assert verdict.allowed and verdict.policy_sha256
    pol.write_text(pol.read_text() + "\n# tightened\n")
    with pytest.raises(RolloutError, match="changed since"):
        start_rollout(store, fleet, p.proposal_id, approver="human:tester")


def _approve_args(pid):
    import argparse
    return argparse.Namespace(proposal_id=pid, as_user=None)


def test_ctl_approve_refuses_without_tty(env, monkeypatch):
    """No TTY (e.g. an agent's shell tool) and no --yes: approval is refused."""
    from cli import ctl
    store, fleet = env
    p, _ = _make_proposal(env)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit, match="interactive"):
        ctl.cmd_approve(store, fleet, _approve_args(p.proposal_id))
    assert store.get_proposal(p.proposal_id)["status"] == "pending_approval"


def test_ctl_approve_refuses_uncommitted_policy(env, monkeypatch):
    from cli import ctl
    store, fleet = env
    p, _ = _make_proposal(env)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(ctl, "policy_uncommitted", lambda: True)
    with pytest.raises(SystemExit, match="uncommitted"):
        ctl.cmd_approve(store, fleet, _approve_args(p.proposal_id))


def test_ctl_approve_requires_typed_confirmation(env, monkeypatch):
    from cli import ctl
    store, fleet = env
    p, _ = _make_proposal(env)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(ctl, "policy_uncommitted", lambda: False)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    ctl.cmd_approve(store, fleet, _approve_args(p.proposal_id))
    assert store.get_proposal(p.proposal_id)["status"] == "pending_approval"
    monkeypatch.setattr("builtins.input", lambda _: p.proposal_id[-4:])
    ctl.cmd_approve(store, fleet, _approve_args(p.proposal_id))
    assert store.get_proposal(p.proposal_id)["status"] == "applied"


def test_policy_rejected_proposal_can_never_roll_out(env):
    store, fleet = env
    patch = {"exporters": {"otlphttp": {"endpoint": "https://evil.example.com"}}}
    p, _ = _make_proposal(env, patch=patch)
    with pytest.raises(RolloutError):
        start_rollout(store, fleet, p.proposal_id, approver="human:tester")


# The agent's entire surface. Adding a tool here is a talk-level decision:
# it must be read-only, or the single propose tool. Never apply/approve.
EXPECTED_MCP_TOOLS = {
    "fleet_overview", "fleet_list_agents", "fleet_get_config", "query_metrics",
    "query_logs", "get_guardrails", "list_proposals", "get_proposal",
    "propose_config_change",
}


def test_mcp_server_cannot_apply():
    """The MCP surface must contain no approve/apply/rollout tool.

    Checked on the *registered* tools and the module's imports/calls — not by
    grepping source, which `getattr(rollout, "start_" + "rollout")` defeats."""
    import ast
    import asyncio
    import mcp_server.server as srv

    tools = asyncio.run(srv.mcp.list_tools())
    assert {t.name for t in tools} == EXPECTED_MCP_TOOLS
    for t in tools:
        if t.name == "propose_config_change":
            assert t.annotations.readOnlyHint is False
            assert t.annotations.destructiveHint is False
        else:
            assert t.annotations.readOnlyHint is True, t.name

    tree = ast.parse(open(srv.__file__).read())
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and (m.startswith("control_plane.rollout") or m.startswith("cli"))
                   for m in imported)
    called = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    called |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not called & {"apply_patch", "rollback", "start_rollout", "manual_rollback",
                         "getattr", "__import__", "import_module"}


def test_mcp_server_imports():
    """A fresh install must yield a server that starts (catches SDK breaks)."""
    import mcp_server.server  # noqa: F401


@pytest.fixture()
def server(env, monkeypatch):
    """The MCP server module wired to this test's store and fleet."""
    import mcp_server.server as srv
    store, fleet = env
    monkeypatch.setattr(srv, "_store", store)
    monkeypatch.setattr(srv, "_fleet", fleet)
    return srv


def _call(server, tool, args):
    import asyncio
    content, _ = asyncio.run(server.mcp.call_tool(tool, args))
    return json.loads(content[0].text)


def test_propose_via_mcp_creates_pending_proposal_and_audit(env, server):
    """The real propose path: typed args through MCP, policy, store, audit."""
    store, fleet = env
    out = _call(server, "propose_config_change", {
        "reason": "Mitigate checkout sku_id cardinality explosion",
        "selector": DEMO_SELECTOR,
        "config_patch": TRANSFORM_PATCH,
        "evidence": ["query_metrics: checkout 8x", "query_logs: WARN sku_id"],
    })
    assert out["status"] == "pending_approval" and out["matched_agents"] > 0
    assert "approve" not in out["next_step"].split("human")[0]  # never told to self-approve
    p = store.get_proposal(out["proposal_id"])
    assert p["author"] == "ai-agent:mcp" and p["status"] == "pending_approval"
    assert all(a.config_version == 1 for a in fleet.agents())  # nothing applied
    created = [e for e in store.audit_log(10) if e["action"] == "proposal.created"]
    assert created and created[0]["detail"]["proposal_id"] == out["proposal_id"]


def test_propose_via_mcp_records_policy_rejection(env, server):
    store, _ = env
    out = _call(server, "propose_config_change", {
        "reason": "Route telemetry somewhere cheaper, observed cost spike",
        "selector": DEMO_SELECTOR,
        "config_patch": {"exporters": {"otlphttp": {"endpoint": "https://evil.example.com"}}},
        "evidence": ["query_metrics: volume up"],
    })
    assert out["status"] == "policy_rejected"
    assert store.get_proposal(out["proposal_id"])["status"] == "policy_rejected"


@pytest.mark.parametrize("selector", [{}, {"labels": {}}, {"all": True, "agent_ids": ["x"]}])
def test_empty_or_ambiguous_selector_rejected(env, selector):
    _, verdict = _make_proposal(env, selector=selector)
    assert not verdict.allowed
    assert any("exactly one" in r for r in verdict.reasons)


def test_propose_via_mcp_rejects_wrong_types(env, server):
    from mcp.server.fastmcp.exceptions import ToolError
    with pytest.raises(ToolError):
        _call(server, "propose_config_change", {
            "reason": "x" * 30, "selector": DEMO_SELECTOR,
            "config_patch": "not an object", "evidence": ["e"]})


def test_mcp_reads_do_not_leak_the_scenario(env, server):
    """The agent must investigate, not read the answer key."""
    store, fleet = env
    fleet.set_scenario({"active": "cardinality_explosion", "service": "checkout",
                        "label": "sku_id", "multiplier": 8.0})
    out = server.fleet_overview() + server.query_metrics() + server.query_metrics(group_by="agent")
    for leak in ("cardinality_explosion", "sku_id", "scenario", "multiplier", "baseline"):
        assert leak not in out


def test_query_metrics_shows_the_jump_over_time(env, server):
    store, fleet = env
    fleet.set_scenario({"active": "cardinality_explosion", "service": "checkout",
                        "label": "sku_id", "multiplier": 8.0})
    points = json.loads(server.query_metrics(window_minutes=60, step_minutes=5))["points"]
    assert len(points) == 13
    first, last = points[0]["by_service"]["checkout"], points[-1]["by_service"]["checkout"]
    assert last == 8 * first
    assert points[0]["by_service"]["search"] == points[-1]["by_service"]["search"]


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
    """A pipeline referencing an undefined processor: policy allows the paths,
    the collector rejects the config (RemoteConfigStatus FAILED), and the
    canaries are rolled back before anything else sees it."""
    store, fleet = env
    patch = {"service": {"pipelines": {"metrics": {
        "processors": ["batch", "filter/typo-in-name"]}}}}
    p, verdict = _make_proposal(env, patch=patch)
    assert verdict.allowed  # policy can't know it's broken; verification catches it

    rollout = start_rollout(store, fleet, p.proposal_id, approver="human:tester")
    assert rollout.status == "rolled_back"
    assert rollout.verification["all_canaries_healthy"] is False
    errors = list(rollout.verification["remote_config_failed"].values())
    assert 'references processor "filter/typo-in-name" which is not configured' in errors[0]

    # canaries restored and healthy again; nobody else was touched
    for aid in rollout.canary_agent_ids:
        a = fleet.get_agent(aid)
        assert a.healthy and a.remote_config_status["status"] == "APPLIED"
        assert "filter/typo-in-name" not in a.config["service"]["pipelines"]["metrics"]["processors"]
    for aid in set(rollout.matched_agent_ids) - set(rollout.canary_agent_ids):
        assert fleet.get_agent(aid).config_version == 1

    final = store.get_proposal(p.proposal_id)
    assert final["status"] == "rolled_back"


def test_unknown_component_type_is_rejected_by_collector(env):
    store, fleet = env
    patch = {"processors": {"filtr/x": {"note": "checkout"}}}
    p, verdict = _make_proposal(env, patch=patch)
    # outside the allowlist anyway — but the sim's collector would reject it too
    assert not verdict.allowed
    from control_plane.fleet import collector_config_errors
    assert collector_config_errors(patch) == ['processors: unknown type: "filtr" for id: "filtr/x"']


def test_transform_strip_label_mitigation(env):
    """The recommended fix: strip the label, keep checkout's metrics."""
    store, fleet = env
    fleet.set_scenario({"active": "cardinality_explosion",
                        "service": "checkout", "label": "sku_id", "multiplier": 8.0})
    base = fleet.baseline_series_for_agents([a.agent_id for a in fleet.agents()])["checkout"]
    p, verdict = _make_proposal(env, patch=TRANSFORM_PATCH)
    assert verdict.allowed
    rollout = start_rollout(store, fleet, p.proposal_id, approver="human:tester")
    assert rollout.status == "applied"
    assert fleet.get_agent(rollout.canary_agent_ids[0]).remote_config_status["status"] == "APPLIED"
    matched = rollout.matched_agent_ids
    assert fleet.service_series_for_agents(matched)["checkout"] == int(
        1.1 * fleet.baseline_series_for_agents(matched)["checkout"])
    assert fleet.series_by_service()["checkout"] < 8 * base


def test_policy_rejects_dropping_existing_pipeline_processors(env):
    store, fleet = env
    raw = store.get_kv("fleet")
    for a in raw.values():
        a["config"]["processors"]["memory_limiter"] = {"limit_mib": 512}
        a["config"]["service"]["pipelines"]["metrics"]["processors"] = ["memory_limiter", "batch"]
    store.put_kv("fleet", raw)
    _, verdict = _make_proposal(env)  # MITIGATION_PATCH sets [batch, filter/...]
    assert not verdict.allowed
    assert any("memory_limiter" in r for r in verdict.reasons)
    patch = {**MITIGATION_PATCH, "service": {"pipelines": {"metrics": {
        "processors": ["memory_limiter", "batch", "filter/drop-sku-checkout"]}}}}
    _, verdict = _make_proposal(env, patch=patch)
    assert verdict.allowed


def test_overbroad_filter_fails_baseline_gate(env):
    """Dropping all of checkout's metrics shrinks series — and must still fail."""
    store, fleet = env
    fleet.set_scenario({"active": "cardinality_explosion",
                        "service": "checkout", "label": "sku_id", "multiplier": 8.0})
    patch = {
        "processors": {"filter/drop-checkout": {"metrics": {"datapoint": [
            'resource.attributes["service.name"] == "checkout"']}}},
        "service": {"pipelines": {"metrics": {
            "processors": ["batch", "filter/drop-checkout"]}}},
    }
    p, verdict = _make_proposal(env, patch=patch)
    assert verdict.allowed
    rollout = start_rollout(store, fleet, p.proposal_id, approver="human:tester")
    assert rollout.verification["no_series_increase"] is True
    assert rollout.verification["services_below_baseline"] == ["checkout"]
    assert rollout.status == "rolled_back"


def test_unwired_processor_has_no_effect(env):
    """Defining a processor without adding it to the pipeline is a no-op."""
    store, fleet = env
    fleet.set_scenario({"active": "cardinality_explosion",
                        "service": "checkout", "label": "sku_id", "multiplier": 8.0})
    before = fleet.series_by_service()["checkout"]
    patch = {"processors": MITIGATION_PATCH["processors"]}
    p, _ = _make_proposal(env, patch=patch)
    start_rollout(store, fleet, p.proposal_id, approver="human:tester")
    assert fleet.series_by_service()["checkout"] == before


def _applied_rollout(env, name):
    store, fleet = env
    patch = {"processors": {f"transform/{name}": {"note": name}}}
    p, verdict = _make_proposal(env, patch=patch, selector={"agent_ids": ["otelcol-0001"]})
    assert verdict.allowed
    return start_rollout(store, fleet, p.proposal_id, approver="human:tester")


def test_rollback_of_older_rollout_is_refused(env):
    """Undoing A after B must not silently discard B (it used to remove B)."""
    from control_plane.rollout import manual_rollback
    store, fleet = env
    a, b = _applied_rollout(env, "a"), _applied_rollout(env, "b")
    with pytest.raises(RolloutError, match=b.rollout_id):
        manual_rollback(store, fleet, a.rollout_id, "human:tester")
    procs = fleet.get_agent("otelcol-0001").config["processors"]
    assert {"transform/a", "transform/b"} <= set(procs)


def test_rollbacks_unwind_in_order(env):
    from control_plane.rollout import manual_rollback
    store, fleet = env
    original = fleet.get_agent("otelcol-0001").config
    a, b = _applied_rollout(env, "a"), _applied_rollout(env, "b")
    manual_rollback(store, fleet, b.rollout_id, "human:tester")
    assert "transform/b" not in fleet.get_agent("otelcol-0001").config["processors"]
    manual_rollback(store, fleet, a.rollout_id, "human:tester")
    assert fleet.get_agent("otelcol-0001").config == original


def test_manual_rollback_state_checks(env):
    from control_plane.rollout import manual_rollback
    store, fleet = env
    r = _applied_rollout(env, "a")
    with pytest.raises(RolloutError, match="human"):
        manual_rollback(store, fleet, r.rollout_id, "ai-agent:mcp")
    manual_rollback(store, fleet, r.rollout_id, "human:tester")
    with pytest.raises(RolloutError, match="only applied"):
        manual_rollback(store, fleet, r.rollout_id, "human:tester")


def test_approval_revalidates_against_current_fleet(env):
    """The full policy runs again at approval, not just the stored verdict."""
    store, fleet = env
    p, verdict = _make_proposal(env, selector={"labels": {"region": "eu-west-1", "tier": "standard"}})
    assert verdict.allowed
    raw = store.get_kv("fleet")
    for a in raw.values():
        if a["labels"]["region"] == "eu-west-1":
            a["labels"]["region"] = "moved"
    store.put_kv("fleet", raw)
    with pytest.raises(RolloutError, match="matches no agents"):
        start_rollout(store, fleet, p.proposal_id, approver="human:tester")


def test_agentinfo_reads_legacy_state():
    from control_plane.models import AgentInfo
    a = AgentInfo.from_dict({"agent_id": "x", "labels": {}, "config": {},
                             "previous_config": {"old": True}})
    assert a.history == []


def test_audit_trail_records_everything(env):
    store, fleet = env
    p, _ = _make_proposal(env)
    store.audit("ai-agent:test", "proposal.created", {"proposal_id": p.proposal_id})
    start_rollout(store, fleet, p.proposal_id, approver="human:tester")
    actions = [e["action"] for e in store.audit_log(50)]
    for expected in ("rollout.start", "rollout.canary_applied", "rollout.applied"):
        assert expected in actions


# ---------------------------------------------------------------------------
# Shared state across processes


@pytest.mark.parametrize("how", ["ctl_reset", "rm_rf"])
def test_long_lived_server_survives_reset(tmp_path, how):
    """A running MCP server's proposals must reach ctl after `make reset`."""
    import shutil
    db = tmp_path / "state" / "ctl.db"
    server_store = Store(str(db))             # the long-lived MCP server
    SimulatedFleet(server_store, size=10)
    server_store.put_proposal({"proposal_id": "prop-old", "status": "pending_approval",
                               "created_at": 0.0})
    if how == "ctl_reset":
        ctl_store = Store(str(db))
        ctl_store.reset()
        SimulatedFleet(ctl_store, size=10).reset()
    else:
        shutil.rmtree(db.parent)
    server_store.put_proposal({"proposal_id": "prop-new", "status": "pending_approval",
                               "created_at": 1.0})
    seen = [p["proposal_id"] for p in Store(str(db)).list_proposals()]
    assert seen == ["prop-new"]


def test_fleet_rebootstraps_after_reset(env):
    store, fleet = env
    store.reset()
    assert len(fleet.agents()) == 40
