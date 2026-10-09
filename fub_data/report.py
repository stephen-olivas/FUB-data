"""Weekly funnel: leads created in a window -> Unqualified / Qualified / Opportunity."""

from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

from .client import FUBClient
from .config import ROOT
from .people import fetch_activity, fetch_people_in_window
from .rules import LeadContext, classify, required_activity
from .window import Window, parse_ts
from .zips import load_service_zips

CSV_COLUMNS = [
    "fub_id", "name", "created_local", "source", "stage", "assigned_to", "contacted",
    "tags", "zip", "zip_origin", "market", "geo_tier", "pool", "status", "primary_unqualified_reason",
    "all_unqualified_reasons", "unqualified_evidence", "opportunity_evidence",
    "review_flags", "recent_sms", "activity_errors", "fub_link",
]


def run_report(
    client: FUBClient,
    criteria: dict,
    window: Window,
    out_dir: Path,
    with_activity: bool = True,
    max_leads: int | None = None,
) -> dict:
    tz = ZoneInfo(criteria.get("timezone", "America/Los_Angeles"))
    service_zips = load_service_zips(criteria, ROOT)
    kinds = required_activity(criteria) if with_activity else []
    app_url = (os.environ.get("FUB_APP_URL") or criteria.get("fub_app_url") or "").rstrip("/")

    print(f"Window {window.start:%Y-%m-%d} .. {window.end:%Y-%m-%d} (end exclusive)", file=sys.stderr)
    people = fetch_people_in_window(client, window, include_trash=criteria.get("include_trash", True))
    if max_leads:
        people = people[:max_leads]
    print(f"{len(people)} leads created in window; activity: {', '.join(kinds) or 'none'}", file=sys.stderr)

    rule_order = [r["id"] for r in criteria.get("unqualified", [])]
    rule_labels = {r["id"]: r.get("label", r["id"]) for r in criteria.get("unqualified", [])}
    diverted_tags = {t.lower() for t in criteria.get("diverted_tags") or []}
    rows, results, diverted = [], [], []
    for i, p in enumerate(people, 1):
        is_diverted = any(str(t).lower() in diverted_tags for t in p.get("tags") or [])
        activity, errors = (fetch_activity(client, p["id"], kinds) if kinds else ({}, {}))
        ctx = LeadContext(p, activity, service_zips, criteria.get("texts_inbound_only", True),
                          criteria.get("text_fields") or [])
        c = classify(ctx, criteria)
        reasons_sorted = [r for r in rule_order if r in c.unqualified_reasons]
        created = parse_ts(p.get("created"))
        rows.append({
            "fub_id": p.get("id"),
            "name": p.get("name") or " ".join(filter(None, [p.get("firstName"), p.get("lastName")])),
            "created_local": created.astimezone(tz).strftime("%Y-%m-%d %H:%M") if created else "",
            "source": p.get("source") or "",
            "stage": p.get("stage") or "",
            "assigned_to": p.get("assignedTo") or "",
            "contacted": "" if p.get("contacted") is None else ("yes" if p.get("contacted") in (1, True, "1", "true") else "no"),
            "tags": "; ".join(map(str, p.get("tags") or [])),
            "zip": c.zip or "",
            "zip_origin": c.zip_origin or "",
            "market": c.market or ("" if not c.zip else "outside service area"),
            "geo_tier": c.geo_tier or "",
            "pool": "diverted" if is_diverted else "res",
            "status": "Diverted (not sent to RES)" if is_diverted else c.status,
            "primary_unqualified_reason": reasons_sorted[0] if reasons_sorted else "",
            "all_unqualified_reasons": "; ".join(reasons_sorted),
            "unqualified_evidence": " || ".join(f"[{r}] {e}" for r in reasons_sorted for e in c.unqualified_reasons[r]),
            "opportunity_evidence": " || ".join(c.opportunity_evidence),
            "review_flags": "; ".join(c.review_flags),
            "recent_sms": _recent_sms(activity.get("texts")) if c.review_flags else "",
            "activity_errors": "; ".join(f"{k}: {v[:80]}" for k, v in errors.items()),
            "fub_link": f"{app_url}/2/people/view/{p.get('id')}" if app_url else "",
        })
        (diverted if is_diverted else results).append((p, c, reasons_sorted, errors))
        if i % 25 == 0:
            print(f"  classified {i}/{len(people)} ({client.request_count} API calls)", file=sys.stderr)

    summary = summarize(results, window, rule_order, rule_labels, kinds, criteria)
    summary["leads_created_all"] = len(results) + len(diverted)
    summary["diverted_not_sent_to_res"] = len(diverted)
    summary["diverted_tags"] = sorted(criteria.get("diverted_tags") or [])
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"funnel_{window.label}"
    with (out_dir / f"{stem}.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    (out_dir / f"{stem}_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    write_geography_csv(summary, out_dir / f"{stem}_geography.csv")
    md = summary_markdown(summary)
    (out_dir / f"{stem}_summary.md").write_text(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(md + "\n")
    print(md)
    print(f"\nWrote {out_dir / stem}.csv", file=sys.stderr)
    return summary


def _recent_sms(texts: list[dict] | None, n: int = 6, width: int = 160) -> str:
    """Last few texts, oldest first, labeled by direction - for leads flagged for review."""
    if not texts:
        return ""
    msgs = sorted(texts, key=lambda t: t.get("created") or "")[-n:]
    out = []
    for t in msgs:
        who = "LEAD" if t.get("isIncoming") else "US"
        body = " ".join(str(t.get("message") or "").split())[:width]
        out.append(f"[{(t.get('created') or '')[:10]} {who}] {body}")
    return " || ".join(out)


def _pct(n: int, d: int) -> str:
    return f"{(100 * n / d):.1f}%" if d else "-"


def summarize(results, window: Window, rule_order, rule_labels, kinds, criteria: dict | None = None) -> dict:
    total = len(results)
    status = Counter(c.status for _, c, _, _ in results)
    primary = Counter(r[0] for _, _, r, _ in results if r)
    any_reason = Counter(x for _, _, r, _ in results for x in r)
    flags = Counter(f for _, c, _, _ in results for f in c.review_flags)
    errors = Counter(k for *_, e in results for k in e)
    qualified_total = status["Qualified"] + status["Opportunity"]
    not_contacted = sum(1 for p, c, _, _ in results
                        if c.status == "Qualified" and p.get("contacted") in (0, False, "0", "false"))

    criteria = criteria or {}
    tiers = criteria.get("geo_tiers") or []
    tier_labels = {t["id"]: t.get("label", t["id"]) for t in tiers}
    by_market: dict[str, Counter] = defaultdict(Counter)
    by_source: dict[str, Counter] = defaultdict(Counter)
    by_stage: dict[str, Counter] = defaultdict(Counter)
    for p, c, _, _ in results:
        by_stage[p.get("stage") or "(none)"][c.status] += 1
        if c.market:
            by_market[f"{c.market} · {tier_labels.get(c.geo_tier, c.geo_tier or '')}"][c.status] += 1
        else:
            by_market["outside service area" if c.zip else "no zip"][c.status] += 1
        by_source[p.get("source") or "(none)"][c.status] += 1

    return {
        "window_start": window.start.isoformat(),
        "window_end_exclusive": window.end.isoformat(),
        "activity_checked": kinds,
        "leads_created": total,  # leads sent to RES (diverted leads excluded)
        "unqualified": status["Unqualified"],
        "qualified_including_opportunities": qualified_total,
        "qualified_only": status["Qualified"],
        "qualified_not_yet_contacted": not_contacted,
        "opportunities": status["Opportunity"],
        "rates": {
            "qualified_of_created": _pct(qualified_total, total),
            "opportunity_of_qualified": _pct(status["Opportunity"], qualified_total),
            "opportunity_of_created": _pct(status["Opportunity"], total),
        },
        "unqualified_by_primary_reason": {rule_labels[r]: primary[r] for r in rule_order},
        "unqualified_by_any_reason": {rule_labels[r]: any_reason[r] for r in rule_order},
        "review_flags": dict(flags),
        "activity_fetch_errors": dict(errors),
        "by_geography": geography_funnel(results, tiers, criteria.get("core_market_tiers") or []),
        "by_market": {k: dict(v) for k, v in sorted(by_market.items())},
        "by_stage": {k: dict(v) for k, v in sorted(by_stage.items(), key=lambda kv: -sum(kv[1].values()))},
        "by_source": {k: dict(v) for k, v in sorted(by_source.items(), key=lambda kv: -sum(kv[1].values()))},
    }


def _segment(name: str, members, total: int) -> dict:
    """One geography row. Every rate is out of ALL leads created in the window,
    because being in the segment is itself part of qualifying."""
    st = Counter(c.status for _, c, _, _ in members)
    q = st["Qualified"] + st["Opportunity"]
    n = len(members)
    return {"segment": name, "total_leads": total,
            "leads": n, "leads_pct_of_total": _pct(n, total),
            "unqualified": st["Unqualified"],
            "qualified_incl_opps": q, "qualified_pct_of_total": _pct(q, total),
            "opportunities": st["Opportunity"], "opportunities_pct_of_total": _pct(st["Opportunity"], total)}


def geography_funnel(results, tiers: list[dict], core_tiers: list[str]) -> list[dict]:
    """Funnel per geo tier, plus 'core markets overall' and out-of-area rows."""
    out = []
    total = len(results)
    for t in tiers:
        row = _segment(t.get("label", t["id"]), [r for r in results if r[1].geo_tier == t["id"]], total)
        row["core_market"] = t["id"] in core_tiers
        out.append(row)
        # after the last core tier, add the rolled-up core-markets row
        if core_tiers and t["id"] == core_tiers[-1] and len(core_tiers) > 1:
            row = _segment("Core markets overall", [r for r in results if r[1].geo_tier in core_tiers], total)
            row["core_market"] = True
            out.append(row)
    out.append(_segment("Outside all lists (rural / out of area)",
                        [r for r in results if r[1].zip and not r[1].geo_tier], total))
    out.append(_segment("No zip found", [r for r in results if not r[1].zip], total))
    out.append(_segment("All leads", results, total))
    return out


GEO_CSV_COLUMNS = [
    ("Segment", "segment"), ("Total leads", "total_leads"), ("Leads in segment", "leads"),
    ("% of total", "leads_pct_of_total"), ("Qualified", "qualified_incl_opps"),
    ("% of total", "qualified_pct_of_total"), ("Opps", "opportunities"),
    ("% of total", "opportunities_pct_of_total"),
]


def write_geography_csv(summary: dict, path: Path) -> None:
    """Shareable table of the core-market rows (RES core, core metro, overall).
    Pastes cleanly into Sheets / Slack."""
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([h for h, _ in GEO_CSV_COLUMNS])
        for g in summary.get("by_geography", []):
            if g.get("core_market"):
                w.writerow([g[k] for _, k in GEO_CSV_COLUMNS])


def summary_markdown(s: dict) -> str:
    start = s["window_start"][:10]
    end = parse_ts(s["window_end_exclusive"])
    from datetime import timedelta
    last = (end - timedelta(seconds=1)).date().isoformat() if end else ""
    lines = [
        f"## RES funnel: leads created {start} to {last}",
        "",
        "| Stage | Count | Rate |",
        "|---|---:|---:|",
        *([f"| All website leads created | {s['leads_created_all']} | |",
           f"| Diverted to 3rd party ({', '.join(s['diverted_tags'])}) | {s['diverted_not_sent_to_res']} | |"]
          if s.get("diverted_not_sent_to_res") else []),
        f"| **Total leads** | **{s['leads_created']}** | |",
        f"| Unqualified | {s['unqualified']} | |",
        f"| Qualified (incl. opportunities) | {s['qualified_including_opportunities']} | {s['rates']['qualified_of_created']} of total |",
        f"| Opportunity | {s['opportunities']} | {s['rates']['opportunity_of_created']} of total |",
        "",
        f"Qualified but not yet contacted in FUB: {s['qualified_not_yet_contacted']}",
        "",
        "### Unqualified reasons",
        "| Reason | Primary | Any |",
        "|---|---:|---:|",
    ]
    for label, n in s["unqualified_by_primary_reason"].items():
        lines.append(f"| {label} | {n} | {s['unqualified_by_any_reason'].get(label, 0)} |")
    lines += ["", "### Funnel by geography",
              f"Every % is out of the {s['leads_created']} total leads in the window.", "",
              "| Segment | Total leads | Leads in segment | % of total | Qualified | % of total | Opps | % of total |",
              "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for g in s.get("by_geography", []):
        if g["segment"] == "All leads" or (g["leads"] == 0 and g["segment"] == "No zip found"):
            continue
        name = f"**{g['segment']}**" if g["segment"] == "Core markets overall" else g["segment"]
        lines.append(f"| {name} | {g['total_leads']} | {g['leads']} | {g['leads_pct_of_total']} | "
                     f"{g['qualified_incl_opps']} | {g['qualified_pct_of_total']} | "
                     f"{g['opportunities']} | {g['opportunities_pct_of_total']} |")
    lines += ["", "### By market", "| Market / tier | Unqualified | Qualified | Opportunity |", "|---|---:|---:|---:|"]
    for m, c in s["by_market"].items():
        lines.append(f"| {m} | {c.get('Unqualified', 0)} | {c.get('Qualified', 0)} | {c.get('Opportunity', 0)} |")
    lines += ["", "### By FUB stage", "| Stage | Unqualified | Qualified | Opportunity |", "|---|---:|---:|---:|"]
    for st, c in s["by_stage"].items():
        lines.append(f"| {st} | {c.get('Unqualified', 0)} | {c.get('Qualified', 0)} | {c.get('Opportunity', 0)} |")
    lines += ["", "### By source (top 15)", "| Source | Unqualified | Qualified | Opportunity |", "|---|---:|---:|---:|"]
    for src, c in list(s["by_source"].items())[:15]:
        lines.append(f"| {src} | {c.get('Unqualified', 0)} | {c.get('Qualified', 0)} | {c.get('Opportunity', 0)} |")
    if s["review_flags"]:
        lines += ["", "### Needs review"] + [f"- {k}: {v}" for k, v in s["review_flags"].items()]
    if s["activity_fetch_errors"]:
        lines += ["", "### Activity fetch errors (leads affected)"] + [f"- {k}: {v}" for k, v in s["activity_fetch_errors"].items()]
    lines += ["", f"Activity checked: {', '.join(s['activity_checked']) or 'none (person fields only)'}"]
    return "\n".join(lines)
