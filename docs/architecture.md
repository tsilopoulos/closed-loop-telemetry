# Architecture

This document maps the reference implementation to the production architecture
described in the talk, and marks exactly where simulation ends and real
integration begins.

## The loop

1. **Observe** — the AI agent (any MCP client; Claude Code via `.mcp.json`)
   uses read tools: `fleet_overview`, `fleet_list_agents`, `fleet_get_config`,
   `query_metrics`, `query_logs`, `get_guardrails`, `list_proposals`.
2. **Propose** — `propose_config_change` creates a `ConfigProposal`. The
   policy engine validates it *at creation*: config-path allowlist/denylist,
   evidence requirement, protected-agent labels (checked against the agents the
   selector resolves to, not the selector text). Invalid proposals are stored
   as `policy_rejected` and can never be applied.
3. **Approve** — a human runs `ctl approve`. This is the only code path into
   the rollout engine, and it rejects `ai-agent:*` actors defensively.
4. **Actuate** — the rollout engine selects a canary wave (≤ `max_canary_fraction`),
   applies the patch, runs verification gates (canary health, no series
   increase, no service below `min_series_vs_baseline` — fewer series is not
   automatically better), then either promotes to the remaining matched agents or
   auto-rolls-back. Every transition is audit-logged.
5. **Verify (again)** — the agent can watch the outcome via `get_proposal`,
   closing the loop with fresh telemetry.

## Simulated vs. real

| Component | Reference build | Production |
|---|---|---|
| Fleet | `SimulatedFleet` (60 agents, telemetry model) | OTel Collectors under an OpAMP control plane |
| Actuation | in-memory config merge | `ServerToAgent.remote_config` push via the OpAMP server |
| Agent feedback | `remote_config_status` (APPLIED/FAILED + collector error), `healthy`, config hash — same shape as OpAMP | `RemoteConfigStatus`, `ComponentHealth`, `EffectiveConfig` reported by the agent/supervisor |
| Config validation | `collector_config_errors`: undefined processor refs, unknown component types | the collector itself (`otelcol validate` semantics) |
| Metrics | model-derived series counts | PromQL via `backends.adapters.PromCompatibleMetrics` |
| Logs | synthetic lines | SQL/HTTP via `backends.adapters.SQLLogs` |
| Traces | not simulated | `backends.adapters.TraceSearch` |
| Rollout timing | synchronous, sim time | async waves with bake periods |

## OpAMP bridge

`control_plane/opamp_bridge.py` mirrors `SimulatedFleet`'s interface. Two
deliberate decisions:

**The bridge talks to the OpAMP server's management API, not the OpAMP wire
protocol.** The OpAMP server stays the single component with write access to
agents. It can layer its own authz, and the guardrail chain composes instead
of being bypassed.

**The MCP surface doesn't change when the bridge is swapped in**
(`CTL_FLEET=opamp`). The agent's world — read tools plus one propose tool — is
identical against the sim and against production. That is what makes the demo
honest: the guardrails aren't a property of the sim, they're a property of the
architecture.

## Threat model, briefly

The agent is treated as a capable but untrusted operator:

- It cannot reach the fleet: no apply tool exists in its surface (test-pinned).
- It cannot approve: the rollout engine only accepts `human:<name>` approvers,
  and `ctl approve` is interactive-only with a typed confirmation.
- Its surface must be MCP *only*. "No apply tool" means nothing if the same
  agent also has a shell: it could run `ctl approve` or edit the policy
  itself. `make agent` runs Claude Code with the otel-fleet tools and nothing
  else. The TTY/confirmation checks are speed bumps (a same-user process can
  fake a TTY); the real boundary in production is identity — approvals from a
  principal the agent can't act as.
- It cannot move the goalposts: each verdict records the policy's sha256; a
  proposal validated under a different guardrails.yaml can't be approved, and
  `ctl approve` refuses while the policy file has uncommitted changes.
- It cannot redirect telemetry: `exporters` is denylisted — exfiltration via
  config change is the scenario this rule exists for.
- It cannot touch ingestion or auth: `receivers`, `extensions` denylisted.
- It cannot rewire or delete pipelines: only `service.pipelines.*.processors`
  is allowlisted, so `exporters: []` or `pipelines: null` is rejected.
- It cannot "fix" volume by deleting the signal: the baseline gate fails a
  canary where any service drops below half its pre-incident series.
- It cannot go big fast: canary cap + verification + auto-rollback bound the
  blast radius even of an approved bad idea.
- Everything it does is attributable: author + evidence + audit log.

Residual risks (talk material, not solved here): prompt injection via
telemetry content read by the agent (log lines are untrusted input!),
approval fatigue turning humans into rubber stamps, and slow-burn effects
that pass verification gates but degrade quality over days.
