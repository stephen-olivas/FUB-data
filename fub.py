#!/usr/bin/env python3
"""Follow Up Boss lead funnel tools.

  python fub.py discover --days 30          # what fields / tags / stages carry data?
  python fub.py report                      # last complete Mon-Sun week
  python fub.py report --start 2026-09-28   # 7 days starting that date
  python fub.py report --start 2026-09-28 --end 2026-10-04
  python fub.py set-stage leads.xlsx            # dry run: what would change
  python fub.py set-stage leads.xlsx --apply    # bulk-move leads to the sheet's "Change to" stage
  python fub.py set-stage --ids "19,20,21" --stage "New stage" --apply

Needs FUB_API_KEY in the environment (optional: FUB_SYSTEM, FUB_SYSTEM_KEY,
FUB_APP_URL e.g. https://bonushomes.followupboss.com for clickable links).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from fub_data.client import FUBClient, FUBError
from fub_data.config import ROOT, load_criteria
from fub_data.window import build_window


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--start", help="first day, YYYY-MM-DD (local time)")
        p.add_argument("--end", help="last day inclusive, YYYY-MM-DD")
        p.add_argument("--config", help="criteria YAML (default config/criteria.yaml)")
        p.add_argument("--out", default=str(ROOT / "output"), help="output folder (git-ignored)")
        p.add_argument("-v", "--verbose", action="store_true")

    d = sub.add_parser("discover", help="profile which FUB fields are populated")
    common(d)
    d.add_argument("--days", type=int, default=30, help="window length when --start is omitted (ends yesterday)")
    d.add_argument("--sample", type=int, default=30, help="leads to sample for per-lead activity")

    r = sub.add_parser("report", help="weekly Unqualified / Qualified / Opportunity funnel")
    common(r)
    r.add_argument("--days", type=int, default=7, help="window length when --end is omitted")
    r.add_argument("--no-activity", action="store_true", help="person fields only (fast, less accurate)")
    r.add_argument("--max-leads", type=int, help="cap leads processed (for testing)")

    s = sub.add_parser("set-stage", help="bulk-update lead stages from a spreadsheet (.xlsx/.csv)")
    s.add_argument("file", nargs="?", help="sheet with an ID column (e.g. 'Bonus FUB ID') and a 'Change to' column")
    s.add_argument("--ids", help="instead of a sheet: FUB person IDs separated by commas/spaces/newlines (needs --stage)")
    s.add_argument("--expect-stage", help="only move leads currently in this stage (others are skipped unless --force)")
    s.add_argument("--sheet", help="worksheet name (default: first sheet)")
    s.add_argument("--stage", help="move every lead to this stage, ignoring the sheet's 'Change to' column")
    s.add_argument("--apply", action="store_true", help="actually write to FUB (default is a dry run)")
    s.add_argument("--force", action="store_true",
                   help="move leads even if their stage no longer matches the sheet's 'Current stage' column")
    s.add_argument("--limit", type=int, help="only process the first N rows (try a few before the full run)")
    s.add_argument("--out", default=str(ROOT / "output"), help="output folder (git-ignored)")
    s.add_argument("-v", "--verbose", action="store_true")

    cp = sub.add_parser("calls-probe", help="read-only check of what call data (transcripts?) the API returns")
    cp.add_argument("--ids", help="FUB person IDs to check (default: most recent calls in the account)")
    cp.add_argument("--max-calls", type=int, default=300, help="calls to scan when --ids is omitted")
    cp.add_argument("--out", default=str(ROOT / "output"))
    cp.add_argument("-v", "--verbose", action="store_true")

    args = ap.parse_args(argv)

    if args.cmd == "calls-probe":
        import re as _re
        from fub_data.calls_probe import run_calls_probe
        ids = [int(t) for t in _re.split(r"[\s,;]+", args.ids or "") if t.strip().isdigit()] or None
        try:
            run_calls_probe(FUBClient(verbose=args.verbose), Path(args.out), ids=ids, max_calls=args.max_calls)
        except FUBError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.cmd == "set-stage":
        from fub_data.stage_update import run_stage_update
        try:
            client = FUBClient(verbose=args.verbose)
            res = run_stage_update(client, args.file, Path(args.out), sheet=args.sheet, stage=args.stage,
                                   apply=args.apply, force=args.force, limit=args.limit,
                                   ids=args.ids, expect_stage=args.expect_stage)
        except (FUBError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 1 if res["counts"].get("error") else 0

    criteria = load_criteria(args.config)
    tz = criteria.get("timezone", "America/Los_Angeles")

    if args.cmd == "discover" and not args.start:
        end = date.today() - timedelta(days=1)
        window = build_window((end - timedelta(days=args.days - 1)).isoformat(), end.isoformat(), tz=tz)
    else:
        window = build_window(args.start, args.end, days=args.days, tz=tz)

    try:
        client = FUBClient(verbose=args.verbose)
        if args.cmd == "discover":
            from fub_data.discover import run_discovery
            run_discovery(client, criteria, window, Path(args.out), sample_size=args.sample)
        else:
            from fub_data.report import run_report
            run_report(client, criteria, window, Path(args.out),
                       with_activity=not args.no_activity, max_leads=args.max_leads)
    except FUBError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
