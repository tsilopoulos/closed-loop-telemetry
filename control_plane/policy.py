"""Guardrail policy engine.

This is the heart of the talk's guardrail model. Every proposal is validated
against a declarative policy *at creation time* — an AI agent cannot even
register a proposal that touches forbidden config surface. Enforcement points:

1. propose-time: path allowlist/denylist, selector limits, evidence required
2. approve-time: human approval is structurally required (there is no
   API or MCP tool that applies config; only cli/ctl.py calls the rollout engine)
3. rollout-time: canary fraction cap, verification gates, auto-rollback

The policy file is YAML (policy/guardrails.yaml) so changes to what the agent
may touch are reviewable in version control like any other config change.
"""

from __future__ import annotations

import os
from typing import Any

import yaml

from .models import PolicyVerdict

DEFAULT_POLICY_PATH = os.environ.get(
    "CTL_POLICY_PATH",
    os.path.join(os.path.dirname(__file__), "..", "policy", "guardrails.yaml"),
)


def load_policy(path: str | None = None) -> dict[str, Any]:
    with open(path or DEFAULT_POLICY_PATH) as f:
        return yaml.safe_load(f)


def _collect_paths(patch: dict[str, Any], prefix: str = "") -> list[str]:
    """Flatten a JSON merge patch into dotted paths of the keys it touches."""
    paths: list[str] = []
    for key, value in patch.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and value:
            paths.extend(_collect_paths(value, dotted))
        else:
            paths.append(dotted)
    return paths


def _path_matches(path: str, rule: str) -> bool:
    """Rule `processors.filter` matches the path itself, its children, and
    named instances (OTel-style `processors.filter/drop-sku.…`)."""
    return path == rule or path.startswith(rule + ".") or path.startswith(rule + "/")


def validate_proposal(
    *,
    config_patch: dict[str, Any],
    selector: dict[str, Any],
    evidence: list[str],
    reason: str,
    policy: dict[str, Any] | None = None,
) -> PolicyVerdict:
    policy = policy or load_policy()
    reasons: list[str] = []
    touched = _collect_paths(config_patch)

    if not touched:
        reasons.append("Empty config patch: nothing to apply.")

    # 1. Denylist wins over everything.
    for path in touched:
        for rule in policy.get("forbidden_config_paths", []):
            if _path_matches(path, rule):
                reasons.append(
                    f"Path '{path}' is forbidden by rule '{rule}'. "
                    "Exporters, extensions and auth surfaces are human-only."
                )

    # 2. Everything touched must be inside the allowlist.
    allow = policy.get("allowed_config_paths", [])
    for path in touched:
        if not any(_path_matches(path, rule) for rule in allow):
            reasons.append(
                f"Path '{path}' is outside the agent allowlist {allow}."
            )

    # 3. Evidence and reason are mandatory: no telemetry, no proposal.
    if policy.get("require_evidence", True) and not evidence:
        reasons.append(
            "Proposals must cite telemetry evidence (queries/observations). "
            "Run fleet/metrics queries first and attach what you saw."
        )
    if len(reason.strip()) < int(policy.get("min_reason_length", 20)):
        reasons.append("Reason is too short to justify a production config change.")

    # 4. Selector sanity: protected agents can never be targeted.
    protected = policy.get("protected_labels", {})
    sel_labels = selector.get("labels", {})
    for k, v in protected.items():
        if sel_labels.get(k) == v:
            reasons.append(f"Selector targets protected agents ({k}={v}).")

    return PolicyVerdict(allowed=not reasons, reasons=reasons, touched_paths=touched)


def canary_size(matched: int, policy: dict[str, Any] | None = None) -> int:
    policy = policy or load_policy()
    frac = float(policy.get("max_canary_fraction", 0.05))
    return max(1, min(matched, int(round(matched * frac)) or 1))
