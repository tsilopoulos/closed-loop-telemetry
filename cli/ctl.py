"""ctl — the human side of the loop.

This CLI is deliberately the ONLY code path that can turn a proposal into a
rollout. The AI agent's MCP server has no equivalent.

    python3 -m cli.ctl list [--status pending_approval]
    python3 -m cli.ctl show <proposal-id>
    python3 -m cli.ctl approve <proposal-id> [--as you@example.com]   (interactive only)
    python3 -m cli.ctl reject <proposal-id> --note "why"
    python3 -m cli.ctl rollback <rollout-id>
    python3 -m cli.ctl audit [--limit 30]
    python3 -m cli.ctl fleet [--reset]
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime

from control_plane.fleet import get_fleet
from control_plane.models import now
from control_plane.policy import policy_uncommitted
from control_plane.rollout import RolloutError, manual_rollback, start_rollout
from control_plane.store import Store


def _ts(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M:%S")


def _human() -> str:
    return f"human:{getpass.getuser()}"


def cmd_list(store: Store, args) -> None:
    rows = store.list_proposals(args.status)
    if not rows:
        print("No proposals.")
        return
    for p in rows:
        print(f"{p['proposal_id']}  [{p['status']:>16}]  {_ts(p['created_at'])}  "
              f"{p['author']}  — {p['reason'][:70]}")


def cmd_show(store: Store, args) -> None:
    p = store.get_proposal(args.proposal_id)
    if not p:
        sys.exit(f"No proposal {args.proposal_id}")
    if p.get("rollout_id"):
        p["rollout"] = store.get_rollout(p["rollout_id"])
    print(json.dumps(p, indent=2))


def cmd_approve(store: Store, fleet, args) -> None:
    approver = f"human:{args.as_user}" if args.as_user else _human()
    # Speed bumps, not a security boundary: an agent running as your OS user
    # can fake a TTY. The boundary is giving the agent MCP tools only
    # (`make agent`, .claude/agent-sandbox.json). These make the unsafe path
    # loud instead of one flag away.
    if not sys.stdin.isatty():
        sys.exit("Refused: approval is interactive only (no TTY). "
                 "Approvals are a human decision; there is no --yes.")
    if policy_uncommitted():
        sys.exit("Refused: policy/guardrails.yaml has uncommitted changes. "
                 "Guardrail changes go through review before they govern approvals.")
    p = store.get_proposal(args.proposal_id)
    if not p:
        sys.exit(f"No proposal {args.proposal_id}")

    print(f"Proposal {p['proposal_id']} by {p['author']}")
    print(f"  Reason:   {p['reason']}")
    print(f"  Selector: {json.dumps(p['selector'])}")
    print(f"  Patch:    {json.dumps(p['config_patch'])[:400]}")
    print(f"  Evidence: {json.dumps(p['evidence'])[:400]}")
    # Typing part of the id (not "y") forces a look at *which* proposal.
    confirm = p["proposal_id"][-4:]
    answer = input(f"Type '{confirm}' to approve and start the canary rollout: ").strip()
    if answer != confirm:
        print("Aborted.")
        return
    try:
        rollout = start_rollout(store, fleet, args.proposal_id, approver)
    except RolloutError as e:
        sys.exit(f"Refused: {e}")
    print(f"\nRollout {rollout.rollout_id}: {rollout.status}")
    print(f"  canaries: {rollout.canary_agent_ids}")
    print(f"  verification: {json.dumps(rollout.verification, indent=2)}")
    if rollout.status == "rolled_back":
        print("  -> verification FAILED; canaries were rolled back automatically.")
    else:
        print(f"  -> promoted to all {len(rollout.matched_agent_ids)} matched agents.")


def cmd_reject(store: Store, args) -> None:
    p = store.get_proposal(args.proposal_id)
    if not p:
        sys.exit(f"No proposal {args.proposal_id}")
    p["status"] = "rejected"
    p["decided_by"] = _human()
    p["decided_at"] = now()
    store.put_proposal(p)
    store.audit(_human(), "proposal.rejected", {
        "proposal_id": args.proposal_id, "note": args.note,
    })
    print(f"Rejected {args.proposal_id}.")


def cmd_rollback(store: Store, fleet, args) -> None:
    try:
        r = manual_rollback(store, fleet, args.rollout_id, _human())
    except RolloutError as e:
        sys.exit(f"Refused: {e}")
    print(f"Rolled back {r.rollout_id} ({len(r.matched_agent_ids)} agents).")


def cmd_audit(store: Store, args) -> None:
    for e in reversed(store.audit_log(args.limit)):
        print(f"{_ts(e['ts'])}  {e['actor']:<18} {e['action']:<26} "
              f"{json.dumps(e['detail'])[:100]}")


def cmd_fleet(store: Store, fleet, args) -> None:
    if args.reset:
        fleet.reset()
        store.audit(_human(), "fleet.reset", {})
        print("Fleet reset to baseline.")
        return
    agents = fleet.agents()
    unhealthy = [a.agent_id for a in agents if not a.healthy]
    print(f"{len(agents)} agents; unhealthy: {unhealthy or 'none'}")
    print(f"scenario: {json.dumps(fleet.scenario())}")
    print(f"series by service: {json.dumps(fleet.series_by_service())}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="ctl")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list"); p.add_argument("--status")
    p = sub.add_parser("show"); p.add_argument("proposal_id")
    p = sub.add_parser("approve"); p.add_argument("proposal_id")
    p.add_argument("--as", dest="as_user", help="approver name (recorded as human:<name>)")
    p = sub.add_parser("reject"); p.add_argument("proposal_id")
    p.add_argument("--note", default="")
    p = sub.add_parser("rollback"); p.add_argument("rollout_id")
    p = sub.add_parser("audit"); p.add_argument("--limit", type=int, default=30)
    p = sub.add_parser("fleet"); p.add_argument("--reset", action="store_true")

    args = ap.parse_args()
    store = Store()
    fleet = get_fleet(store)

    if args.cmd == "list":
        cmd_list(store, args)
    elif args.cmd == "show":
        cmd_show(store, args)
    elif args.cmd == "approve":
        cmd_approve(store, fleet, args)
    elif args.cmd == "reject":
        cmd_reject(store, args)
    elif args.cmd == "rollback":
        cmd_rollback(store, fleet, args)
    elif args.cmd == "audit":
        cmd_audit(store, args)
    elif args.cmd == "fleet":
        cmd_fleet(store, fleet, args)


if __name__ == "__main__":
    main()
