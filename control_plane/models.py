"""Domain models for the closed-loop control plane.

Core invariant encoded here: a ConfigProposal is *never* applied at creation.
It moves through an explicit lifecycle, and only a human approval transition
can start a rollout.

Proposal lifecycle:

    pending_approval --(human approve)--> canarying --> verifying --> promoting --> applied
          |                                   |             |
          +--(human reject)--> rejected       +-------------+--(failure)--> rolled_back
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

# ---------------------------------------------------------------------------
# Statuses

PROPOSAL_STATUSES = (
    "pending_approval",
    "rejected",
    "canarying",
    "verifying",
    "promoting",
    "applied",
    "rolled_back",
    "policy_rejected",
)

TERMINAL_STATUSES = {"rejected", "applied", "rolled_back", "policy_rejected"}


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def now() -> float:
    return time.time()


# ---------------------------------------------------------------------------


@dataclass
class AgentInfo:
    """A managed telemetry agent (an OTel Collector in the real system)."""

    agent_id: str
    labels: dict[str, str]  # e.g. {"env": "prod", "region": "us-east-1"}
    config: dict[str, Any]  # effective collector-style config
    healthy: bool = True  # OpAMP ComponentHealth.healthy
    config_version: int = 1
    # OpAMP RemoteConfigStatus as the agent reports it: status is APPLIED |
    # APPLYING | FAILED, with the hash of the remote config it refers to and
    # the collector's own error message when it rejected the config.
    remote_config_status: dict[str, Any] = field(
        default_factory=lambda: {"status": "APPLIED", "last_remote_config_hash": None,
                                 "error_message": ""})
    # Stack of configs this agent had before each rollout touched it, newest
    # last: [{"rollout_id", "config", "healthy", "remote_config_status"}]. A rollout can be rolled
    # back on an agent only while it is the newest entry, so undoing an older
    # rollout can never silently discard a newer one.
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "AgentInfo":
        # Tolerate state written by older versions (e.g. `previous_config`).
        known = AgentInfo.__dataclass_fields__
        return AgentInfo(**{k: v for k, v in d.items() if k in known})


@dataclass
class PolicyVerdict:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    touched_paths: list[str] = field(default_factory=list)
    policy_sha256: str | None = None  # fingerprint of the policy that decided

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConfigProposal:
    """An agent-authored *suggestion* to change fleet configuration.

    Creating one has zero effect on the fleet. Only `ctl approve` (a human
    action) turns it into a Rollout.
    """

    proposal_id: str
    created_at: float
    author: str  # "ai-agent:<name>" or "human:<user>"
    reason: str  # natural-language justification, required
    selector: dict[str, Any]  # which agents this targets
    config_patch: dict[str, Any]  # JSON merge patch applied to agent config
    evidence: list[str]  # telemetry queries / observations backing the change
    status: str = "pending_approval"
    policy_verdict: dict[str, Any] | None = None
    decided_by: str | None = None
    decided_at: float | None = None
    rollout_id: str | None = None

    @staticmethod
    def create(
        author: str,
        reason: str,
        selector: dict[str, Any],
        config_patch: dict[str, Any],
        evidence: list[str] | None = None,
    ) -> "ConfigProposal":
        return ConfigProposal(
            proposal_id=new_id("prop"),
            created_at=now(),
            author=author,
            reason=reason,
            selector=selector,
            config_patch=config_patch,
            evidence=evidence or [],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ConfigProposal":
        return ConfigProposal(**d)


@dataclass
class Rollout:
    """The staged application of an approved proposal.

    canary -> verify -> promote, with automatic rollback on verification
    failure. Every stage transition is written to the audit log.
    """

    rollout_id: str
    proposal_id: str
    created_at: float
    matched_agent_ids: list[str]
    canary_agent_ids: list[str]
    status: str = "canarying"  # canarying | verifying | promoting | applied | rolled_back
    verification: dict[str, Any] = field(default_factory=dict)
    finished_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Rollout":
        return Rollout(**d)
