"""Guardrail policy engine.

This is the heart of the talk's guardrail model. Every proposal is validated
against a declarative policy *at creation time* — an AI agent cannot even
register a proposal that touches forbidden config surface. Enforcement points:

1. propose-time: path allowlist/denylist, protected agents (checked against
   the resolved agent set, not the selector text), evidence required
2. approve-time: human approval is structurally required (there is no
   API or MCP tool that applies config; only cli/ctl.py calls the rollout engine)
3. rollout-time: protected agents re-checked against the fleet as it is now,
   canary fraction cap, verification gates, auto-rollback

The policy file is YAML (policy/guardrails.yaml) so changes to what the agent
may touch are reviewable in version control like any other config change.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from typing import Any, Callable

import yaml

from .models import AgentInfo, PolicyVerdict

DEFAULT_POLICY_PATH = os.environ.get(
    "CTL_POLICY_PATH",
    os.path.join(os.path.dirname(__file__), "..", "policy", "guardrails.yaml"),
)


def load_policy(path: str | None = None) -> dict[str, Any]:
    with open(path or DEFAULT_POLICY_PATH) as f:
        return yaml.safe_load(f)


def policy_fingerprint(path: str | None = None) -> str:
    """sha256 of the policy file. Recorded in every verdict and re-checked at
    approval, so a verdict is only honoured under the policy that issued it."""
    with open(path or DEFAULT_POLICY_PATH, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def policy_uncommitted(path: str | None = None) -> bool | None:
    """True if the policy file differs from git HEAD (e.g. an agent with file
    access edited it), None if git can't tell (not a checkout, no git)."""
    path = os.path.abspath(path or DEFAULT_POLICY_PATH)
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain", "--", os.path.basename(path)],
            cwd=os.path.dirname(path), capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return bool(r.stdout.strip()) if r.returncode == 0 else None


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
    """Segment-wise prefix match. A rule segment matches the same key or a
    named instance of it (`filter` matches `filter/drop-sku`); `*` matches
    any one key. The path must be at least as deep as the rule, so rule
    `service.pipelines.*.processors` does NOT allow `service.pipelines: null`
    or rewiring a pipeline's exporters."""
    ps, rs = path.split("."), rule.split(".")
    if len(ps) < len(rs):
        return False
    return all(r == "*" or p == r or p.startswith(r + "/") for p, r in zip(ps, rs))


def protected_targets(targets: list[AgentInfo],
                      policy: dict[str, Any] | None = None) -> list[str]:
    """IDs of resolved agents carrying any protected label.

    Checked against the agents a selector actually matches, not the selector
    text: `{"labels": {"service": "checkout"}}` or `{"all": true}` must not
    reach a payment-critical collector just because it doesn't say "tier".
    """
    policy = policy or load_policy()
    protected = policy.get("protected_labels", {})
    return [
        a.agent_id for a in targets
        if any(a.labels.get(k) == v for k, v in protected.items())
    ]


TELEMETRY_TOOLS = {"query_metrics", "query_logs"}


def _evidence_problems(evidence: list[Any], policy: dict[str, Any],
                       receipts: Callable[[str], dict[str, Any] | None] | None,
                       as_of: float | None) -> list[str]:
    """Evidence must cite receipts the server issued for real reads — the
    agent can't hallucinate a query it never ran."""
    cfg = policy.get("evidence", {}) or {}
    if not evidence or not cfg.get("require_receipts", False):
        return []
    as_of = as_of or time.time()
    max_age = float(cfg.get("max_age_minutes", 60)) * 60
    problems, tools = [], set()
    for i, item in enumerate(evidence):
        rid = item.get("receipt_id") if isinstance(item, dict) else None
        r = receipts(rid) if (rid and receipts) else None
        if r is None:
            problems.append(f"Evidence #{i + 1} cites no valid receipt ({rid!r}). "
                            "Cite the evidence_receipt returned by a read tool.")
        elif as_of - r["issued_at"] > max_age:
            problems.append(f"Evidence receipt {rid} is older than "
                            f"{cfg.get('max_age_minutes', 60)} minutes; re-query.")
        else:
            tools.add(r["tool"])
    if cfg.get("require_telemetry", True) and not problems and not tools & TELEMETRY_TOOLS:
        problems.append("Evidence must include at least one telemetry query "
                        f"({', '.join(sorted(TELEMETRY_TOOLS))}).")
    return problems


def validate_proposal(
    *,
    config_patch: dict[str, Any],
    selector: dict[str, Any],
    targets: list[AgentInfo],
    evidence: list[Any],
    reason: str,
    policy: dict[str, Any] | None = None,
    receipts: Callable[[str], dict[str, Any] | None] | None = None,
    as_of: float | None = None,
) -> PolicyVerdict:
    """`targets` is the fleet's resolution of `selector` at proposal time.
    `receipts` resolves evidence receipt ids; `as_of` is when the proposal
    was made (receipt age is judged then, not at approval)."""
    policy_is_default = policy is None
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
    reasons.extend(_evidence_problems(evidence, policy, receipts, as_of))
    if len(reason.strip()) < int(policy.get("min_reason_length", 20)):
        reasons.append("Reason is too short to justify a production config change.")

    # 3b. JSON merge patch replaces lists wholesale: `processors: [batch, x]`
    # silently drops anything else already in that pipeline (a memory_limiter,
    # say) on every agent where it differs. Additions must keep what's there.
    if policy.get("preserve_pipeline_processors", True):
        for pname, pipe in ((config_patch.get("service") or {}).get("pipelines") or {}).items():
            new = (pipe or {}).get("processors") if isinstance(pipe, dict) else None
            if not isinstance(new, list):
                continue
            dropped = sorted({
                proc for a in targets
                for proc in (a.config.get("service", {}).get("pipelines", {})
                             .get(pname, {}).get("processors", []))
                if proc not in new
            })
            if dropped:
                reasons.append(
                    f"Patch replaces service.pipelines.{pname}.processors and would "
                    f"remove {dropped} on matched agents (merge patch replaces lists). "
                    "Include the existing processors in the new list.")

    # 4. Selector sanity: explicit, matches something, never a protected agent.
    modes = [k for k in ("labels", "agent_ids", "all") if selector.get(k)]
    if len(modes) != 1 or set(selector) - {"labels", "agent_ids", "all"}:
        reasons.append(
            f"Selector {selector} must set exactly one of labels, agent_ids, or "
            "all:true (an empty selector must never mean 'everything').")
    if not targets:
        reasons.append(f"Selector {selector} matches no agents.")
    if hit := protected_targets(targets, policy):
        protected = policy.get("protected_labels", {})
        reasons.append(
            f"Selector matches protected agents {hit} "
            f"({', '.join(f'{k}={v}' for k, v in protected.items())}). "
            "Narrow the selector to exclude them, e.g. add a label that "
            "only non-protected agents carry."
        )

    return PolicyVerdict(allowed=not reasons, reasons=reasons, touched_paths=touched,
                         policy_sha256=policy_fingerprint() if policy_is_default else None)


def canary_size(matched: int, policy: dict[str, Any] | None = None) -> int:
    policy = policy or load_policy()
    frac = float(policy.get("max_canary_fraction", 0.05))
    return max(1, min(matched, int(round(matched * frac)) or 1))
