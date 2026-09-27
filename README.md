# Closing the Loop

[![ci](https://github.com/tsilopoulos/closed-loop-telemetry/actions/workflows/ci.yml/badge.svg)](https://github.com/tsilopoulos/closed-loop-telemetry/actions/workflows/ci.yml)

Reference implementation for **"Closing the Loop: AI Agents Driving an
OpenTelemetry Fleet With MCP and OpAMP"** (Observability Summit Europe 2026).

An AI agent gets **read-everything, apply-nothing** access to an OpenTelemetry
collector fleet through an MCP server. It can *propose* configuration changes;
a policy engine validates them, a human approves them, and a rollout engine
applies them via canary with verification gates and automatic rollback — the
role OpAMP plays in a real deployment.

```
             MCP (read + propose)                     human approval
 AI agent ──────────────────────► control plane ◄──────────────────── ctl CLI
   ▲                                   │ policy engine (guardrails.yaml)
   │ telemetry evidence                │ rollout engine: canary → verify → promote
   │                                   ▼        └── auto-rollback on failure
 metrics / logs / fleet state     collector fleet   (SimulatedFleet | OpAMPBridge)
```

Everything runs locally with zero infrastructure via a simulated fleet. The
`OpAMPBridge` seam (`control_plane/opamp_bridge.py`) is where a real OpAMP
control plane plugs in without touching the agent-facing surface.

## Quickstart

```bash
make setup               # .venv + deps (Python 3.11+); .mcp.json uses .venv/bin/python
make test                # closed-loop + guardrail invariant tests
make demo                # trigger a cardinality explosion in `checkout`
```

Then run the loop:

```bash
# 1. The AI agent side — connect any MCP client to mcp_server/server.py.
#    With Claude Code: `make agent` starts it with the otel-fleet tools ONLY
#    (no shell, no file edits — see .claude/agent-sandbox.json).
#    Prompt: "Telemetry volume looks wrong. Investigate and fix it."
#    The agent will query metrics/logs, read the guardrails, and call
#    propose_config_change. Nothing is applied.

# 2. The human side:
python3 -m cli.ctl list
python3 -m cli.ctl show <proposal-id>
python3 -m cli.ctl approve <proposal-id>     # interactive; canary → verify → promote
python3 -m cli.ctl audit                     # the full trail
```

Failure paths worth demoing: propose an `exporters` change (**policy_rejected**
— redirecting telemetry is on the denylist), or a pipeline that references a
processor that isn't defined (the canaries reject it exactly like a collector
would — OpAMP `RemoteConfigStatus: FAILED` with the collector's error — →
**automatic rollback**).

No network or model on stage? `make replay` plays the agent's side through
the real MCP tools (including the first, policy-rejected selector), then hands
you the real interactive `ctl approve`.

Two more scenarios for the talk's "failure modes" and "restraint" beats:

```bash
python3 -m scenarios.trigger prompt_injection   # a log line tells the agent to move the
                                                # exporter; even if it obeys, policy_rejected
python3 -m scenarios.trigger traffic_growth     # every service +30% from scale-out; nothing
                                                # is wrong — the right answer is no proposal
```

## The guardrail model (what the talk is actually about)

| Gate | Where | What it stops |
|---|---|---|
| Path allowlist/denylist | `policy/guardrails.yaml` | Agent touching exporters, receivers, auth, or deleting/rewiring pipelines — only telemetry-shaping processors and pipeline processor lists are proposable |
| Evidence receipts | policy engine | Proposals not grounded in observed telemetry — evidence must cite receipts the MCP server issued for real reads (the human sees what the query returned, not the agent's paraphrase) |
| Protected labels | policy engine + rollout engine | Reaching payment-critical agents at all — checked on the agents a selector resolves to, at propose and again at approve time |
| Human approval | `cli/ctl.py` only | Autonomous application — no MCP tool for it; approver must be `human:<name>`, interactive, typed confirmation, under the exact (committed) policy that validated the proposal |
| Canary cap (≤5%) | rollout engine | Fleet-wide blast radius on first contact |
| Verification gates | rollout engine | Promoting configs that hurt — both directions: unhealthy canaries, series increase, *and* any service dropping below 50% of baseline (over-broad filters, pipelines that stop delivering) |
| Auto-rollback + audit log | rollout engine / store | Silent failures and unaccountable changes |

## Layout

```
mcp_server/       the agent-facing MCP server (stdio)
control_plane/    models, store, policy, rollout engine, fleet, OpAMP bridge
cli/              ctl — human approval CLI
policy/           guardrails.yaml — the reviewable agent contract
scenarios/        demo perturbations (cardinality explosion, prompt injection, traffic growth, incident)
backends/         adapter stubs for real metrics/traces/logs backends
tests/            closed-loop + guardrail invariant tests
```

See `CLAUDE.md` for invariants, the demo script, and the roadmap.

## Status

Reference implementation / talk companion — not production software. The
rollout engine runs synchronously in sim time; the OpAMP bridge and backend
adapters are documented stubs. It is early days, deliberately and honestly.
