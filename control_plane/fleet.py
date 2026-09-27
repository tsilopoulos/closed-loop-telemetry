"""Fleet controllers.

`FleetController` is the seam between this reference implementation and a real
OpAMP control plane:

- `SimulatedFleet` — default. N in-memory collectors with a small telemetry
  model, so the whole closed loop (detect -> propose -> approve -> canary ->
  verify -> promote) runs end-to-end with zero infrastructure. Scenarios
  (scenarios/trigger.py) perturb the model to create incidents to detect.

- `OpAMPBridge` (control_plane/opamp_bridge.py) — adapter stub that forwards
  the same operations to a real OpAMP server. Swap it in via CTL_FLEET=opamp.

The telemetry model is intentionally simple: each service emits some number of
active metric series through every agent that hosts it. A "cardinality
explosion" scenario multiplies a service's series. Processors wired into the
metrics pipeline reshape that (see `_processor_factor`): stripping or
filtering the exploded label brings it back down, over-broad filters drop the
service entirely, and a pipeline without receivers/exporters delivers nothing
— on exactly the agents where the patch is applied, which is what makes
canary verification meaningful in both directions.
"""

from __future__ import annotations

import copy
import random
import re
import time
from typing import Any

from .models import AgentInfo
from .store import Store

FLEET_KEY = "fleet"
SCENARIO_KEY = "scenario"
HISTORY_KEY = "series_history"
HISTORY_MAX = 500

SERVICES = ["checkout", "inventory", "fulfillment", "order-mgmt", "search"]
REGIONS = ["us-east-1", "eu-west-1", "ap-southeast-1"]
ENVS = ["prod", "staging"]

BASE_SERIES_PER_AGENT = {
    "checkout": 1200,
    "inventory": 900,
    "fulfillment": 800,
    "order-mgmt": 1000,
    "search": 700,
}

DEFAULT_AGENT_CONFIG: dict[str, Any] = {
    "receivers": {"otlp": {"protocols": {"grpc": {}, "http": {}}}},
    "processors": {"batch": {"timeout": "5s"}},
    "exporters": {"otlphttp": {"endpoint": "https://gateway.internal:4318"}},
    "service": {
        "pipelines": {
            "metrics": {"receivers": ["otlp"], "processors": ["batch"], "exporters": ["otlphttp"]},
            "traces": {"receivers": ["otlp"], "processors": ["batch"], "exporters": ["otlphttp"]},
            "logs": {"receivers": ["otlp"], "processors": ["batch"], "exporters": ["otlphttp"]},
        }
    },
}


def _merge_patch(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """RFC 7386-style JSON merge patch (None deletes a key)."""
    out = copy.deepcopy(target)
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_patch(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _processor_factor(name: str, proc_cfg: Any, service: str,
                      scenario: dict[str, Any]) -> float:
    """Multiplicative effect of one *wired* processor on `service`'s series.

    A deliberately small model of real processor semantics:
    - A processor scopes itself to a service if its config mentions
      `service.name`; otherwise it applies to every service on the agent.
    - filter: conditions naming the exploded label drop just the datapoints
      that carry it (the offending metric goes away, ~0.9x of baseline);
      conditions that don't narrow by label drop the service's metrics
      entirely (0x) — which is what verification must catch.
    - transform/attributes naming the exploded label strip it: the series
      collapse back to ~baseline (1.1x, allowing some residual).
    - samplers act on traces/logs, not metrics.
    """
    text = str(proc_cfg)
    if "service.name" in text and service not in text:
        return 1.0
    base = name.split("/")[0]
    exploding = scenario.get("active") == "cardinality_explosion" and scenario.get("service") == service
    label = scenario.get("label", "sku_id")
    mult = float(scenario.get("multiplier", 8.0)) if exploding else 1.0
    if base == "filter":
        # Narrowed by a datapoint attribute (not just resource/service scope)?
        dp_text = re.sub(r"resource\.attributes\[[^\]]*\]", "", text)
        if "attributes[" in dp_text:
            return 0.9 / mult if exploding and label in dp_text else 1.0
        return 0.0
    if base in {"transform", "attributes"} and exploding and label in text:
        return 1.1 / mult
    return 1.0


def _metrics_pipeline_processors(config: dict[str, Any]) -> list[str] | None:
    """Processor names in the metrics pipeline, or None if the pipeline
    can't deliver anything (missing, or no receivers/exporters)."""
    pipe = config.get("service", {}).get("pipelines", {}).get("metrics")
    if not pipe or not pipe.get("receivers") or not pipe.get("exporters"):
        return None
    return list(pipe.get("processors", []))


def _agent_series(config: dict[str, Any], services: list[str],
                  scenario: dict[str, Any]) -> dict[str, int]:
    """Series each hosted service delivers to the backend through this agent."""
    wired = _metrics_pipeline_processors(config)
    processors = config.get("processors", {})
    out: dict[str, int] = {}
    for s in services:
        if wired is None:
            out[s] = 0
            continue
        exploding = scenario.get("active") == "cardinality_explosion" and scenario.get("service") == s
        factor = float(scenario.get("multiplier", 8.0)) if exploding else 1.0
        # Only processors wired into the pipeline do anything — defining one
        # without adding it to `service.pipelines.metrics.processors` is a
        # classic real-world no-op.
        for name in wired:
            if name in processors:
                factor *= _processor_factor(name, processors[name], s, scenario)
        out[s] = int(BASE_SERIES_PER_AGENT[s] * factor)
    return out


class SimulatedFleet:
    """A deterministic-ish simulated fleet backed by the shared Store."""

    def __init__(self, store: Store, size: int = 60, seed: int = 7):
        self.store = store
        if self.store.get_kv(FLEET_KEY) is None:
            self._bootstrap(size, seed)

    # -- bootstrap ------------------------------------------------------------

    def _bootstrap(self, size: int, seed: int) -> None:
        rng = random.Random(seed)
        agents: dict[str, Any] = {}
        for i in range(size):
            env = "prod" if i % 5 else "staging"
            agent = AgentInfo(
                agent_id=f"otelcol-{i:04d}",
                labels={
                    "env": env,
                    "region": rng.choice(REGIONS),
                    "services": ",".join(sorted(rng.sample(SERVICES, k=rng.randint(2, 4)))),
                    "tier": "payment-critical" if i % 17 == 0 else "standard",
                },
                config=copy.deepcopy(DEFAULT_AGENT_CONFIG),
            )
            agents[agent.agent_id] = agent.to_dict()
        self.store.put_kv(FLEET_KEY, agents)
        self.store.put_kv(SCENARIO_KEY, {"active": None})
        self.store.put_kv(HISTORY_KEY, [])
        self._record()

    def reset(self, size: int = 60, seed: int = 7) -> None:
        self._bootstrap(size, seed)

    # -- reads ---------------------------------------------------------------

    def agents(self) -> list[AgentInfo]:
        raw = self.store.get_kv(FLEET_KEY, {})
        return [AgentInfo.from_dict(a) for a in raw.values()]

    def get_agent(self, agent_id: str) -> AgentInfo | None:
        raw = self.store.get_kv(FLEET_KEY, {})
        return AgentInfo.from_dict(raw[agent_id]) if agent_id in raw else None

    def select(self, selector: dict[str, Any]) -> list[AgentInfo]:
        agents = self.agents()
        if selector.get("all"):
            return agents
        if ids := selector.get("agent_ids"):
            wanted = set(ids)
            return [a for a in agents if a.agent_id in wanted]
        labels: dict[str, str] = selector.get("labels", {})
        out = []
        for a in agents:
            ok = True
            for k, v in labels.items():
                if k == "service":
                    ok = ok and v in a.labels.get("services", "").split(",")
                else:
                    ok = ok and a.labels.get(k) == v
            if ok:
                out.append(a)
        return out

    # -- scenario + telemetry model -------------------------------------------

    def scenario(self) -> dict[str, Any]:
        return self.store.get_kv(SCENARIO_KEY, {"active": None})

    def set_scenario(self, scenario: dict[str, Any]) -> None:
        self.store.put_kv(SCENARIO_KEY, scenario)
        self._record()

    # -- history: the sim's stand-in for a metrics backend's time dimension --

    def _record(self) -> None:
        """Snapshot per-service series whenever the model changes, so queries
        can show *when* something moved rather than just its current value."""
        hist = self.store.get_kv(HISTORY_KEY, [])
        hist.append({"ts": time.time(), "by_service": self.series_by_service()})
        self.store.put_kv(HISTORY_KEY, hist[-HISTORY_MAX:])

    def series_history(self, window_s: int, step_s: int) -> list[dict[str, Any]]:
        """Step-function series per service over [now - window, now]."""
        hist = self.store.get_kv(HISTORY_KEY, []) or [
            {"ts": 0.0, "by_service": self.series_by_service()}]
        t_now = time.time()
        points, i = [], 0
        for k in range(window_s // step_s, -1, -1):
            t = t_now - k * step_s
            while i + 1 < len(hist) and hist[i + 1]["ts"] <= t:
                i += 1
            points.append({"ts": round(t), "by_service": hist[i]["by_service"]})
        return points

    def _service_multiplier(self, service: str) -> float:
        sc = self.scenario()
        if sc.get("active") == "cardinality_explosion" and sc.get("service") == service:
            return float(sc.get("multiplier", 8.0))
        return 1.0

    @staticmethod
    def _services(a: AgentInfo) -> list[str]:
        return [s for s in a.labels.get("services", "").split(",") if s]

    def series_by_service(self) -> dict[str, int]:
        """Total active series per service across the fleet, mitigation-aware."""
        return self.service_series_for_agents([a.agent_id for a in self.agents()])

    def service_series_for_agents(self, agent_ids: list[str]) -> dict[str, int]:
        """Series per service delivered by the given agents."""
        wanted, sc = set(agent_ids), self.scenario()
        totals = {s: 0 for s in SERVICES}
        for a in self.agents():
            if a.agent_id in wanted:
                for s, n in _agent_series(a.config, self._services(a), sc).items():
                    totals[s] += n
        return totals

    def baseline_series_for_agents(self, agent_ids: list[str]) -> dict[str, int]:
        """Pre-incident series per service for the given agents.

        Real deployment: the same query over a trailing window before the
        anomaly (e.g. last week, same hour)."""
        wanted = set(agent_ids)
        totals = {s: 0 for s in SERVICES}
        for a in self.agents():
            if a.agent_id in wanted:
                for s in self._services(a):
                    totals[s] += BASE_SERIES_PER_AGENT[s]
        return totals

    def series_for_agents(self, agent_ids: list[str]) -> int:
        return sum(self.service_series_for_agents(agent_ids).values())

    def recent_logs(self, service: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        sc = self.scenario()
        logs: list[dict[str, Any]] = []
        t = time.time()
        rng = random.Random(int(t) // 60)
        for i in range(limit):
            svc = service or rng.choice(SERVICES)
            level, msg = "INFO", f"{svc}: request completed"
            if sc.get("active") == "cardinality_explosion" and svc == sc.get("service"):
                if i % 3 == 0:
                    level = "WARN"
                    msg = (
                        f"{svc}: metrics client registered high-cardinality label "
                        f"'{sc.get('label', 'sku_id')}' ({rng.randint(40_000, 90_000)} distinct values)"
                    )
            if sc.get("active") == "incident" and svc == sc.get("service"):
                if i % 2 == 0:
                    level = "ERROR"
                    msg = f"{svc}: upstream timeout after 5000ms (incident {sc.get('incident_id')})"
            logs.append({"ts": t - i * 13, "service": svc, "level": level, "message": msg})
        return logs

    # -- writes (only the rollout engine calls these) ---------------------------

    def apply_patch(self, agent_ids: list[str], patch: dict[str, Any]) -> None:
        raw = self.store.get_kv(FLEET_KEY, {})
        for aid in agent_ids:
            a = AgentInfo.from_dict(raw[aid])
            a.previous_config = copy.deepcopy(a.config)
            a.config = _merge_patch(a.config, patch)
            a.config_version += 1
            # Sim: a patch containing "__break__" renders the agent unhealthy,
            # so tests and demos can exercise the auto-rollback path.
            a.healthy = "__break__" not in str(patch)
            raw[aid] = a.to_dict()
        self.store.put_kv(FLEET_KEY, raw)
        self._record()

    def rollback(self, agent_ids: list[str]) -> None:
        raw = self.store.get_kv(FLEET_KEY, {})
        for aid in agent_ids:
            a = AgentInfo.from_dict(raw[aid])
            if a.previous_config is not None:
                a.config = a.previous_config
                a.previous_config = None
                a.config_version += 1
            a.healthy = True
            raw[aid] = a.to_dict()
        self.store.put_kv(FLEET_KEY, raw)
        self._record()


def get_fleet(store: Store):
    """Factory honoring CTL_FLEET (sim | opamp)."""
    import os

    if os.environ.get("CTL_FLEET", "sim") == "opamp":
        from .opamp_bridge import OpAMPBridge

        return OpAMPBridge(store)
    return SimulatedFleet(store)
