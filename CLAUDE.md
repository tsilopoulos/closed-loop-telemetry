# CLAUDE.md

Reference implementation for the talk **"Closing the Loop: AI Agents Driving an
OpenTelemetry Fleet With MCP and OpAMP"** (Observability Summit Europe 2026).

An AI agent observes an OTel collector fleet through MCP, proposes config
changes, and a human-approved control plane applies them via canary rollout —
modeling an OpAMP-managed fleet.

## Architecture (30 seconds)

- `mcp_server/server.py` — the ONLY surface the AI agent sees. Read tools +
  one propose tool. **No apply/approve tool exists here, ever.**
- `control_plane/policy.py` + `policy/guardrails.yaml` — validates proposals
  at creation: path allowlist/denylist, evidence required, protected agents.
- `cli/ctl.py` — human approval CLI; the only path into the rollout engine.
- `control_plane/rollout.py` — canary (≤5%) → verify → promote, auto-rollback.
- `control_plane/fleet.py` — `SimulatedFleet` (default, zero infra) and the
  seam to a real control plane: `control_plane/opamp_bridge.py` (`CTL_FLEET=opamp`).
- `backends/adapters.py` — stubs for real metrics/traces/logs backends.
- Shared state: SQLite at `.state/ctl.db` (delete it to reset everything).

## Invariants — do not break these, they ARE the talk

1. The MCP server must never gain a tool that applies, approves, or rolls out
   config. `tests/test_loop.py::test_mcp_server_cannot_apply` pins this.
2. `start_rollout` must reject non-human approvers (allowlist: `human:<name>`)
   and verdicts issued under a different guardrails.yaml than the current one.
3. Every mutation goes through the policy engine and is audit-logged.
4. `policy/guardrails.yaml` changes are treated like production config review.
5. Keep everything vendor-neutral at the protocol level (OTLP, OpAMP, MCP);
   backend-specific code lives only in `backends/` and `opamp_bridge.py`.

## Commands

```bash
make setup       # .venv + requirements-dev.txt (required by .mcp.json)
make test        # pytest
make demo        # trigger cardinality explosion + print fleet state
make reset       # wipe .state/
python3 -m cli.ctl list|show|approve|reject|rollback|audit|fleet
python3 -m scenarios.trigger cardinality_explosion --service checkout
```

The MCP server for this repo is registered in `.mcp.json`; in a Claude Code
session the `otel-fleet` tools are available directly — you (Claude) are the
"AI agent" in the loop.

## Demo script (for the talk)

1. `make reset && make demo` — checkout series jump ~8x.
2. `make agent` (Claude Code restricted to the otel-fleet MCP tools): "Something is wrong with our telemetry volume. Investigate
   and fix it." → agent uses fleet_overview / query_metrics / query_logs,
   reads get_guardrails, calls propose_config_change.
3. Human: `python3 -m cli.ctl show <id>` then `approve <id>` — watch canary →
   verify → promote in the output; `ctl audit` shows the full trail.
4. Failure path: have the agent propose an exporter change (policy_rejected)
   or use a `__break__` patch (auto-rollback after canary).

## Roadmap (rough priority order)

- [ ] Async rollout engine: bake time between canary/promote waves, poll-based
      verification instead of synchronous checks.
- [ ] Implement `OpAMPBridge` against a real OpAMP server management API
      (opamp-go example server first; then the production control plane).
- [ ] Implement `backends/adapters.py` against real backends and route the
      MCP query tools through them when `CTL_FLEET=opamp`.
- [ ] Multi-wave rollouts (5% → 25% → 100%) with per-wave verification.
- [ ] Web UI for the approval queue (replace/augment ctl).
- [ ] Proposal expiry + dedup (agent shouldn't re-propose an open change).
- [ ] Evaluations per mcp-builder practice: 10 read-only Q&A tasks against a
      seeded fleet to measure agent effectiveness with these tools.

## Conventions

- Python 3.11+ (CI runs 3.11–3.14), stdlib + `mcp` (<2) + `pyyaml` only (keep the
  dependency surface tiny). `pytest` lives in `requirements-dev.txt`.
- Type hints everywhere; dataclasses for domain models; JSON-serializable state.
- New guardrails need: a yaml knob, enforcement in policy.py or rollout.py,
  and a test.
