"""Trigger demo scenarios that perturb the simulated fleet's telemetry.

    python3 -m scenarios.trigger cardinality_explosion --service checkout --label sku_id
    python3 -m scenarios.trigger incident --service fulfillment
    python3 -m scenarios.trigger clear
"""

from __future__ import annotations

import argparse
import json
import uuid

from control_plane.fleet import get_fleet
from control_plane.store import Store


def main() -> None:
    ap = argparse.ArgumentParser(prog="trigger")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("cardinality_explosion")
    p.add_argument("--service", default="checkout")
    p.add_argument("--label", default="sku_id")
    p.add_argument("--multiplier", type=float, default=8.0)

    p = sub.add_parser("incident")
    p.add_argument("--service", default="fulfillment")

    sub.add_parser("clear")

    args = ap.parse_args()
    store = Store()
    fleet = get_fleet(store)

    if args.cmd == "cardinality_explosion":
        fleet.set_scenario({
            "active": "cardinality_explosion",
            "service": args.service,
            "label": args.label,
            "multiplier": args.multiplier,
        })
        store.audit("scenario", "scenario.cardinality_explosion", {
            "service": args.service, "label": args.label, "multiplier": args.multiplier,
        })
    elif args.cmd == "incident":
        fleet.set_scenario({
            "active": "incident",
            "service": args.service,
            "incident_id": f"INC-{uuid.uuid4().hex[:6]}",
        })
        store.audit("scenario", "scenario.incident", {"service": args.service})
    else:
        fleet.set_scenario({"active": None})
        store.audit("scenario", "scenario.clear", {})

    print(json.dumps(fleet.scenario(), indent=2))
    print(json.dumps(fleet.series_by_service(), indent=2))


if __name__ == "__main__":
    main()
