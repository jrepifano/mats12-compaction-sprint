"""Week-2 plan check: does a config still match its proposal's Build contract? $0, no network.

    uv run python scripts/week2_plan_check.py <proposal.md> <config.toml> [--json]
        Compare every contract parameter with the config, and the proposal's sha256 with the
        config's meta.built_against (listing the sections changed since the accepted copy).
        Exit 0 when they match, 1 on drift.
    uv run python scripts/week2_plan_check.py --accept-plan <sha> <proposal.md> <config.toml>
        After accounting for a proposal change: re-record built_against (sha must be the
        proposal's current sha256), store the accepted copy under configs/week2/accepted/ and
        append a delta table to logs/s2/week2_build_log.md. Exit 0/1. The contract check
        still applies afterwards.
    uv run python scripts/week2_plan_check.py <proposal.md> <config.toml> --design-card <out.md>
        (two_phase configs) Write the design card of the runs the code will build, after the
        check; exit 1 without writing on drift.
    uv run python scripts/week2_plan_check.py <proposal.md> <config.toml> --plan-diff
        The unified diff from the accepted copy to the current proposal, for the astra code
        review prompt (next to the code diff).

Library: compaction.s2.chain.week2.
"""

from __future__ import annotations

import argparse
import json
import sys

from compaction.s2.chain import week2 as W


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("proposal")
    ap.add_argument("config")
    ap.add_argument("--json", action="store_true", help="print the check as JSON")
    ap.add_argument("--accept-plan", metavar="SHA", help="re-record built_against as SHA")
    ap.add_argument("--design-card", metavar="OUT", help="write the design card (two_phase)")
    ap.add_argument("--plan-diff", action="store_true",
                    help="print the diff from the accepted copy to the current proposal")
    a = ap.parse_args(argv)
    if a.plan_diff:
        sys.stdout.write(W.plan_diff(a.proposal, a.config))
        return 0
    if a.accept_plan:
        try:
            res = W.accept_plan(a.proposal, a.config, a.accept_plan)
        except ValueError as e:
            print(f"refused: {e}", file=sys.stderr)
            return 1
        if a.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"accepted {res['exp']}: {res['old']} -> {res['new']}")
            print(f"  changed sections: {', '.join(res['changed_sections']) or '(none)'}")
            print(f"  accepted copy: {res['accepted_copy']}")
            print(f"  fill in the delta table in {res['log']}")
        check = W.plan_check(a.proposal, a.config)
        if not check["ok"]:  # the hash is recorded, but the contract still differs
            print(W.report(check), file=sys.stderr)
            return 1
        return 0
    try:
        check = W.plan_check(a.proposal, a.config)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(check, indent=2) if a.json else W.report(check))
    if not check["ok"]:
        return 1
    if a.design_card:
        cfg = W.load_config(a.config)
        if cfg["design"]["kind"] != "two_phase":
            print("--design-card here builds two_phase runs only", file=sys.stderr)
            return 1
        W.design_card(cfg, W.two_phase_rows(cfg), a.design_card)
        print(f"design card: {a.design_card}", file=sys.stderr if a.json else sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
