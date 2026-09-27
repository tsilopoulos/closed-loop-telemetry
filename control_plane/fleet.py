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
explosion" scenario multiplies a service's series. A filter/transform
processor patch that references the service brings its multiplier back down —
on exactly the agents where the patch is applied, which is what makes canary
verification meaningful.
"""

from __future__ import annotations

import copy
import random
import time
from typing import Any

from .models import AgentInfo
from .store import Store

FLEET_KEY = "fleet"
SCENARIO_KEY = "scenario"

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


def _patch_mitigates(config: dict[str, Any], service: str) -> bool:
    """Does this agent config contain a processor that tames `service`?

    Heuristic for the sim: any allowlisted shaping processor whose config
    mentions the service name counts as a mitigation.
    """
    processors = config.get("processors", {})
    for name, proc_cfg in processors.items():
        base = name.split("/")[0]
        if base in {"filter", "transform", "attributes", "probabilistic_sampler", "tail_sampling"}:
            if service in str(proc_cfg):
                return True
    return False


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

    def _service_multiplier(self, service: str) -> float:
        sc = self.scenario()
        if sc.get("active") == "cardinality_explosion" and sc.get("service") == service:
            return float(sc.get("multiplier", 8.0))
        return 1.0

    def series_by_service(self) -> dict[str, int]:
        """Total active series per service across the fleet, mitigation-aware."""
        totals = {s: 0 for s in SERVICES}
        for a in self.agents():
            hosted = a.labels.get("services", "").split(",")
            for s in hosted:
                if not s:
                    continue
                mult = self._service_multiplier(s)
                if mult > 1.0 and _patch_mitigates(a.config, s):
                    mult = 1.1  # mitigated agents shed the exploded labels
                totals[s] += int(BASE_SERIES_PER_AGENT[s] * mult)
        return totals

    def series_for_agents(self, agent_ids: list[str]) -> int:
        wanted = set(agent_ids)
        total = 0
        for a in self.agents():
            if a.agent_id not in wanted:
                continue
            for s in a.labels.get("services", "").split(","):
                if not s:
                    continue
                mult = self._service_multiplier(s)
                if mult > 1.0 and _patch_mitigates(a.config, s):
                    mult = 1.1
                total += int(BASE_SERIES_PER_AGENT[s] * mult)
        return total

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


def get_fleet(store: Store):
    """Factory honoring CTL_FLEET (sim | opamp)."""
    import os

    if os.environ.get("CTL_FLEET", "sim") == "opamp":
        from .opamp_bridge import OpAMPBridge

        return OpAMPBridge(store)
    return SimulatedFleet(store)
