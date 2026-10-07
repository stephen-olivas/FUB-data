"""Field discovery: what data does FUB actually carry for recent leads?

Answers "which fields are consistently populated?" so the qualification rules
in config/criteria.yaml can point at real tags, stages and custom fields.
The report avoids personal details: it shows value distributions only for
categorical fields (stage, source, tags, dropdown custom fields, ...) and
shows coverage only for free-text and contact fields.
"""

from __future__ import annotations

import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from .client import FUBClient
from .config import ROOT
from .people import ACTIVITY_ENDPOINTS, fetch_activity, fetch_people_in_window
from .rules import TEXT_SOURCES, LeadContext, _keyword_patterns, classify, evaluate_matchers
from .window import Window
from .zips import extract_zips, load_service_zips

CATEGORICAL_PERSON_FIELDS = {
    "stage", "source", "createdVia", "contacted", "assignedTo", "claimed", "delayed",
    "assignedPondId", "assignedLenderName", "dealStatus", "dealStage", "timeframe",
}
CATEGORICAL_CF_TYPES = {"dropdown", "multiselect", "checkbox", "select", "boolean", "radio"}
# Never show values for these (contact details / free-form personal data).
PII_FIELDS = {"name", "firstName", "lastName", "emails", "phones", "addresses", "picture",
              "socialData", "collaborators", "background", "customSlackThreadLink", "sourceUrl"}
# Other fields show their values only when they behave like a category:
# few distinct values that each repeat (so no single lead's text is exposed).
MAX_DISTINCT = 25
TOP_N = 25


def _empty(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _type(v) -> str:
    return type(v).__name__


def _schema(records: list[dict]) -> dict[str, dict]:
    """Key -> {pct populated, types} across a list of dicts (one level + `property.*`)."""
    n = len(records)
    pop: Counter = Counter()
    types: dict[str, set] = defaultdict(set)
    for r in records:
        for k, v in r.items():
            if isinstance(v, dict) and k in ("property", "person"):
                for k2, v2 in v.items():
                    key = f"{k}.{k2}"
                    types[key].add(_type(v2))
                    if not _empty(v2):
                        pop[key] += 1
                continue
            types[k].add(_type(v))
            if not _empty(v):
                pop[k] += 1
    return {k: {"populated_pct": round(100 * pop[k] / n, 1) if n else 0, "types": sorted(types[k])}
            for k in sorted(types)}


def _show_values(k: str, values: dict[str, Counter], always_show: set[str]) -> bool:
    if k not in values or not values[k]:
        return False
    if k in always_show:
        return True
    c = values[k]
    if k.endswith("Id") or k in ("id", "created", "updated", "lastActivity", "lastLeadActivity"):
        return False
    distinct, total = len(c), sum(c.values())
    return distinct <= MAX_DISTINCT and min(c.values()) >= 2 and distinct <= total / 3


def _first_words(c: Counter | None) -> list | None:
    """For free-text fields: distribution of the first word (letters only), repeated words only."""
    if not c:
        return None
    fw: Counter = Counter()
    for val, n in c.items():
        m = re.match(r"\s*([A-Za-z][A-Za-z'/-]{0,14})", val)
        fw[m.group(1).lower() if m else "(non-text)"] += n
    top = [(w, n) for w, n in fw.most_common(12) if n >= 3 and w != "(non-text)"]
    return top or None


def run_discovery(
    client: FUBClient,
    criteria: dict,
    window: Window,
    out_dir: Path,
    sample_size: int = 30,
    seed: int = 7,
) -> dict:
    out: dict = {"window_start": window.start.isoformat(), "window_end_exclusive": window.end.isoformat()}

    # --- account reference data ---------------------------------------------
    print("Reading account reference data (stages, custom fields, users)...", file=sys.stderr)
    stages, err = client.try_list("stages", "stages")
    out["stages"] = [{"id": s.get("id"), "name": s.get("name")} for s in stages] or err
    cfs, err = client.try_list("customFields", "customfields")
    out["custom_fields"] = [
        {"name": c.get("name"), "label": c.get("label"), "type": c.get("type"),
         "choices": c.get("choices")} for c in cfs
    ] or err
    users, err = client.try_list("users", "users")
    out["user_count"] = len(users) if users else err

    # --- people in window --------------------------------------------------
    print("Pulling people created in window...", file=sys.stderr)
    people = fetch_people_in_window(client, window, include_trash=criteria.get("include_trash", True))
    n = len(people)
    out["people_in_window"] = n
    print(f"  {n} people", file=sys.stderr)

    cf_types = {c["name"]: (c.get("type") or "").lower() for c in out["custom_fields"]} if isinstance(out["custom_fields"], list) else {}
    coverage: Counter = Counter()
    types: dict[str, set] = defaultdict(set)
    values: dict[str, Counter] = defaultdict(Counter)
    tag_counts: Counter = Counter()
    always_show: set[str] = set()
    for p in people:
        for k, v in p.items():
            types[k].add(_type(v))
            if _empty(v):
                continue
            coverage[k] += 1
            is_cf = k.startswith("custom")
            categorical = (k in CATEGORICAL_PERSON_FIELDS) or (is_cf and cf_types.get(k) in CATEGORICAL_CF_TYPES)
            if k == "tags":
                tag_counts.update(map(str, v))
            elif k not in PII_FIELDS and not isinstance(v, dict):
                vals = v if isinstance(v, list) else [v]
                values[k].update(str(x).strip()[:80] for x in vals)
                if categorical:
                    always_show.add(k)
    out["person_fields"] = {
        k: {
            "populated_pct": round(100 * coverage[k] / n, 1) if n else 0,
            "types": sorted(types[k]),
            "custom_field_type": cf_types.get(k),
            "distinct_values": len(values[k]) if k in values else None,
            "top_values": values[k].most_common(TOP_N) if _show_values(k, values, always_show) else None,
            "top_first_words": _first_words(values.get(k)) if not _show_values(k, values, always_show) else None,
        }
        for k in sorted(types, key=lambda k: (-coverage[k], k))
    }
    out["tags"] = tag_counts.most_common(100)

    # --- geography ---------------------------------------------------------
    service = load_service_zips(criteria, ROOT)
    geo = Counter()
    states = Counter()
    for p in people:
        zs = extract_zips(p)
        if not zs:
            geo["no zip on person record"] += 1
        else:
            row = service.get(zs[0][0])
            geo[f"in service: {row['market']}" if row else "outside service zips"] += 1
        for a in p.get("addresses") or []:
            if isinstance(a, dict) and a.get("state"):
                states[str(a["state"]).upper()] += 1
    out["geography_person_address"] = dict(geo.most_common())
    out["address_states"] = states.most_common(20)

    # --- activity sample ---------------------------------------------------
    rng = random.Random(seed)
    sample = rng.sample(people, min(sample_size, n)) if n else []
    print(f"Sampling activity for {len(sample)} people (events, notes, texts, calls, appointments, deals)...", file=sys.stderr)
    kinds = list(ACTIVITY_ENDPOINTS)
    per_kind: dict[str, list[dict]] = defaultdict(list)
    has_any: Counter = Counter()
    errs: Counter = Counter()
    contexts = []
    for p in sample:
        act, e = fetch_activity(client, p["id"], kinds)
        for k in kinds:
            per_kind[k].extend(act.get(k, []))
            if act.get(k):
                has_any[k] += 1
        errs.update(e.keys())
        contexts.append(LeadContext(p, act, service, criteria.get("texts_inbound_only", True),
                                    criteria.get("text_fields") or []))

    activity_out = {}
    for k in kinds:
        recs = per_kind[k]
        entry = {
            "people_with_any_pct": round(100 * has_any[k] / len(sample), 1) if sample else 0,
            "records": len(recs),
            "fetch_errors": errs[k],
            "schema": _schema(recs),
        }
        if k == "events":
            entry["by_type"] = Counter(str(r.get("type")) for r in recs).most_common(TOP_N)
            entry["by_source"] = Counter(str(r.get("source")) for r in recs).most_common(TOP_N)
            entry["property_types"] = Counter(
                str((r.get("property") or {}).get("type")) for r in recs if isinstance(r.get("property"), dict)
            ).most_common(TOP_N)
            geo_ev = Counter()
            for c in contexts:
                zs = extract_zips(c.person, c.activity.get("events"))
                ev_z = [z for z, o in zs if o == "event_property"]
                if ev_z:
                    row = service.get(ev_z[0])
                    geo_ev[f"in service: {row['market']}" if row else "outside service zips"] += 1
                else:
                    geo_ev["no property zip on events"] += 1
            entry["geography_event_property"] = dict(geo_ev)
        if k == "texts":
            entry["direction_field_present"] = any(("isIncoming" in r) or ("direction" in r) for r in recs)
        if k == "appointments":
            entry["by_type"] = Counter(str(r.get("type") or r.get("title")) for r in recs).most_common(TOP_N)
        if k == "deals":
            entry["by_stage"] = Counter(str(r.get("stageName") or r.get("stage")) for r in recs).most_common(TOP_N)
        activity_out[k] = entry
    out["activity_sample"] = {"sample_size": len(sample), "by_kind": activity_out}

    # --- config check + rule preview on the sample --------------------------
    out["config_check"] = config_check(criteria, out, tag_counts, people)
    out["rule_preview_on_sample"] = rule_preview(criteria, contexts)

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"discovery_{window.label}"
    (out_dir / f"{stem}.json").write_text(json.dumps(out, indent=2, default=str))
    md = discovery_markdown(out)
    (out_dir / f"{stem}.md").write_text(md)
    print(md)
    print(f"\nWrote {out_dir / stem}.md and .json ({client.request_count} API calls)", file=sys.stderr)
    return out


def config_check(criteria: dict, out: dict, tag_counts: Counter, people: list[dict]) -> list[str]:
    """Flag configured values that don't exist in the account / window."""
    notes = []
    stage_names = {s["name"].lower() for s in out["stages"]} if isinstance(out["stages"], list) else set()
    cf_names = {c["name"] for c in out["custom_fields"]} if isinstance(out["custom_fields"], list) else set()
    seen_tags = {t.lower() for t in tag_counts}
    sources = {str(p.get("source") or "").lower() for p in people}
    rules = [(r["id"], r.get("any_of", {})) for r in criteria.get("unqualified", [])]
    rules.append(("opportunity", criteria.get("opportunity", {}).get("any_of", {})))
    for rid, spec in rules:
        for s in spec.get("stages") or []:
            if stage_names and s.lower() not in stage_names:
                notes.append(f"[{rid}] stage '{s}' does not exist in this FUB account")
        for t in spec.get("tags") or []:
            if t.lower() not in seen_tags:
                notes.append(f"[{rid}] tag '{t}' not used on any lead in the window")
        for f in (spec.get("custom_fields") or {}):
            if cf_names and f not in cf_names:
                notes.append(f"[{rid}] custom field '{f}' does not exist")
        for src in spec.get("sources") or []:
            if not any(src.lower() in s for s in sources):
                notes.append(f"[{rid}] source '{src}' not seen in the window")
        has_signal = any(spec.get(k) for k in ("tags", "stages", "custom_fields", "sources", "property_types",
                                                  "keywords", "zip_outside_service_area", "has_appointment", "has_deal"))
        if not has_signal:
            notes.append(f"[{rid}] has no matchers configured; it can never fire")
    return notes


def rule_preview(criteria: dict, contexts: list[LeadContext]) -> dict:
    """How often each rule / keyword fires on the sample."""
    n = len(contexts)
    rules = [(r["id"], r.get("any_of", {})) for r in criteria.get("unqualified", [])]
    rules.append(("opportunity", criteria.get("opportunity", {}).get("any_of", {})))
    res = {}
    for rid, spec in rules:
        fired = sum(1 for c in contexts if evaluate_matchers(spec, c))
        kw_hits = Counter()
        sources = spec.get("search_in") or list(TEXT_SOURCES)
        for c in contexts:
            blobs = c.text_blobs()
            for kw, pat in _keyword_patterns(spec.get("keywords") or []):
                if any(pat.search(t) for s in sources for t in blobs.get(s, [])):
                    kw_hits[kw] += 1
        res[rid] = {"leads_matched": fired, "of_sample": n, "keyword_hits": kw_hits.most_common()}
    status = Counter(classify(c, criteria).status for c in contexts)
    res["_classification"] = dict(status)
    return res


def discovery_markdown(o: dict) -> str:
    L = [f"# FUB field discovery: leads created {o['window_start'][:10]} to {o['window_end_exclusive'][:10]} (end exclusive)",
         "", f"People in window: **{o['people_in_window']}**  ·  Users: {o['user_count']}", ""]

    L += ["## Person fields (sorted by coverage)", "| Field | Populated | Distinct | Type | Top values |", "|---|---:|---:|---|---|"]
    for k, v in o["person_fields"].items():
        tv = v["top_values"]
        tv_s = ", ".join(f"{val} ({c})" for val, c in tv[:8]) if tv else ""
        if not tv and v.get("top_first_words"):
            tv_s = "first word: " + ", ".join(f"{w} ({c})" for w, c in v["top_first_words"][:8])
        typ = "/".join(v["types"]) + (f" (custom: {v['custom_field_type']})" if v.get("custom_field_type") else "")
        dv = v.get("distinct_values")
        L.append(f"| `{k}` | {v['populated_pct']}% | {'' if dv is None else dv} | {typ} | {_md(tv_s)} |")

    L += ["", "## Tags used in window", ", ".join(f"{_md(t)} ({c})" for t, c in o["tags"][:60]) or "(none)"]

    L += ["", "## Stages in account"]
    L.append(", ".join(s["name"] for s in o["stages"]) if isinstance(o["stages"], list) else f"error: {o['stages']}")

    L += ["", "## Custom fields in account", "| Name | Label | Type | Choices |", "|---|---|---|---|"]
    if isinstance(o["custom_fields"], list):
        for c in o["custom_fields"]:
            ch = ", ".join(map(str, c.get("choices") or []))[:120]
            L.append(f"| `{c['name']}` | {_md(c.get('label') or '')} | {c.get('type')} | {_md(ch)} |")
    else:
        L.append(f"error: {o['custom_fields']}")

    L += ["", "## Geography (person address)"] + [f"- {k}: {v}" for k, v in o["geography_person_address"].items()]
    L.append("States: " + ", ".join(f"{s} ({c})" for s, c in o["address_states"]))

    a = o["activity_sample"]
    L += ["", f"## Activity on a sample of {a['sample_size']} leads"]
    for k, e in a["by_kind"].items():
        L += ["", f"### {k}: {e['people_with_any_pct']}% of sampled leads have any ({e['records']} records"
              + (f", {e['fetch_errors']} fetch errors" if e["fetch_errors"] else "") + ")"]
        for extra in ("by_type", "by_source", "property_types", "by_stage"):
            if e.get(extra):
                L.append(f"- {extra}: " + ", ".join(f"{_md(t)} ({c})" for t, c in e[extra][:12]))
        if e.get("geography_event_property"):
            L.append("- property zip: " + ", ".join(f"{k2} ({v})" for k2, v in e["geography_event_property"].items()))
        if k == "texts":
            L.append(f"- direction field present: {e.get('direction_field_present')}")
        if e["schema"]:
            L.append("- fields: " + ", ".join(f"`{f}` {s['populated_pct']}%" for f, s in e["schema"].items()))

    L += ["", "## Config check (config/criteria.yaml)"]
    L += [f"- {x}" for x in o["config_check"]] or ["- all configured values exist"]

    L += ["", "## Rule preview on sample", "| Rule | Leads matched | Keyword hits |", "|---|---:|---|"]
    for rid, r in o["rule_preview_on_sample"].items():
        if rid.startswith("_"):
            continue
        kws = ", ".join(f"{_md(k)} ({c})" for k, c in r["keyword_hits"]) or "-"
        L.append(f"| {rid} | {r['leads_matched']}/{r['of_sample']} | {kws} |")
    L.append("")
    L.append("Sample classification: " + ", ".join(f"{k} {v}" for k, v in o["rule_preview_on_sample"]["_classification"].items()))
    return "\n".join(L)


def _md(s: str) -> str:
    return re.sub(r"\|", "/", str(s))
