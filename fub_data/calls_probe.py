"""Check what call data the FUB API returns, before building a transcript export.

FUB shows transcripts and AI summaries in the app for calls made through a FUB
Number with call recording on, but the API docs don't list any transcript
field ("some call data is not accessible via API"). This probe reads real call
records and reports:

  - every field on /calls records and how often it's filled (list + detail view)
  - any field that looks like a transcript, summary or recording, with text lengths
  - whether notes on the same leads carry call summaries / transcripts
  - whether a few likely sub-endpoints (calls/{id}/transcript, ...) exist

Read-only. Writes output/calls_probe_<time>.md|json. With GITHUB_ACTIONS set it
also emits ::notice:: lines (field names and counts only, no lead content) so the
findings are readable from the run's annotations.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from .client import FUBClient, FUBError

INTERESTING = re.compile(r"transcri|summar|record|audio|media|ai|sentiment|recap|note", re.I)
TEXTY = re.compile(r"transcri|summar", re.I)
SUB_ENDPOINTS = ["transcript", "transcription", "summary", "recording"]


def _flatten(d, prefix=""):
    for k, v in (d or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            yield from _flatten(v, key + ".")
        else:
            yield key, v


def _filled(v) -> bool:
    return v not in (None, "", [], {}, 0, False)


def _shape(v) -> str:
    if isinstance(v, str):
        return f"str[{len(v)}]"
    if isinstance(v, list):
        return f"list[{len(v)}]"
    return type(v).__name__


def _duration(c) -> float:
    try:
        return float(c.get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


def run_calls_probe(client: FUBClient, out_dir: Path, ids: list[int] | None = None,
                    max_calls: int = 300, detail_n: int = 8) -> dict:
    calls: list[dict] = []
    if ids:
        for pid in ids:
            calls += client.list_all("calls", "calls", {"personId": pid})
    else:
        for c in client.paginate("calls", {}, "calls"):
            calls.append(c)
            if len(calls) >= max_calls:
                break
    print(f"{len(calls)} call records", file=sys.stderr)

    list_fields: Counter = Counter()
    shapes: dict[str, Counter] = {}
    for c in calls:
        for k, v in _flatten(c):
            if _filled(v):
                list_fields[k] += 1
                shapes.setdefault(k, Counter())[_shape(v) if not isinstance(v, str) or len(v) < 40 else "str[40+]"] += 1

    durations = [_duration(c) for c in calls]
    long_calls = sorted((c for c in calls if _duration(c) >= 20), key=_duration, reverse=True)

    # Detail view of the longest calls: may carry fields the list view omits.
    detail_fields: Counter = Counter()
    detail_samples = []
    sub_hits: dict[str, Counter] = {s: Counter() for s in SUB_ENDPOINTS}
    for c in long_calls[:detail_n]:
        cid = c.get("id")
        try:
            d = client.get(f"calls/{cid}")
        except FUBError as exc:
            detail_samples.append({"id": cid, "error": str(exc)[:200]})
            continue
        flat = dict(_flatten(d))
        for k, v in flat.items():
            if _filled(v):
                detail_fields[k] += 1
        detail_samples.append({
            "id": cid, "duration": c.get("duration"),
            "fields": {k: _shape(v) for k, v in flat.items() if _filled(v)},
            # values only for short non-text fields; text (notes, summaries, URLs) as its length
            "interesting": {k: (v if not isinstance(v, str) or (len(v) <= 40 and not re.search(r"transcri|summar|note|url", k, re.I))
                                else f"<text {len(v)} chars>")
                            for k, v in flat.items() if INTERESTING.search(k) and _filled(v)},
        })
        for s in SUB_ENDPOINTS:
            try:
                r = client.get(f"calls/{cid}/{s}")
                sub_hits[s]["200 " + ",".join(sorted(r)[:6])] += 1
            except FUBError as exc:
                m = re.search(r"-> (\d{3})", str(exc))
                sub_hits[s][m.group(1) if m else "error"] += 1

    # Notes on the same leads: FUB may log summaries/transcripts as notes.
    note_hits = Counter()
    note_examples = []
    people = []
    for c in long_calls:
        pid = c.get("personId")
        if pid and pid not in people:
            people.append(pid)
        if len(people) >= detail_n:
            break
    for pid in people:
        try:
            notes = client.list_all("notes", "notes", {"personId": pid})
        except FUBError:
            continue
        for n in notes:
            text = f"{n.get('subject') or ''} {n.get('body') or ''}"
            for word in ("transcript", "summary", "call summary", "action items"):
                if word in text.lower():
                    note_hits[word] += 1
            if TEXTY.search(text) and len(note_examples) < 5:
                note_examples.append({"personId": pid, "subject": (n.get("subject") or "")[:80],
                                      "body_chars": len(n.get("body") or ""), "type": n.get("type")})

    texty_list = sorted(k for k in list_fields if TEXTY.search(k))
    texty_detail = sorted(k for k in detail_fields if TEXTY.search(k))
    recording = sorted(k for k in set(list_fields) | set(detail_fields) if re.search(r"record", k, re.I))
    verdict = (
        "TRANSCRIPT FIELD FOUND: " + ", ".join(texty_list or texty_detail)
        if (texty_list or texty_detail) else
        "No transcript/summary field on /calls records" + (" (recording field present: " + ", ".join(recording) + ")" if recording else "")
    )

    result = {
        "calls_checked": len(calls),
        "calls_20s_plus": len(long_calls),
        "max_duration_s": max(durations) if durations else 0,
        "verdict": verdict,
        "list_fields": {k: {"filled": n, "shapes": dict(shapes[k])} for k, n in list_fields.most_common()},
        "detail_fields": dict(detail_fields.most_common()),
        "detail_samples": detail_samples,
        "sub_endpoints": {s: dict(v) for s, v in sub_hits.items()},
        "notes_mentioning": dict(note_hits),
        "note_examples": note_examples,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (out_dir / f"calls_probe_{stamp}.json").write_text(json.dumps(result, indent=2, default=str))
    md = [f"# FUB call data probe\n", f"**{verdict}**\n",
          f"Calls checked: {len(calls)} · 20s+: {len(long_calls)} · longest: {result['max_duration_s']:.0f}s\n",
          "## Fields on /calls (list view)\n", "| Field | Filled | Shapes |", "|---|---:|---|"]
    md += [f"| `{k}` | {v['filled']} | {', '.join(f'{s} ({n})' for s, n in v['shapes'].items())} |"
           for k, v in result["list_fields"].items()]
    md += ["\n## Fields on /calls/{id} (detail view, longest calls)\n",
           ", ".join(f"`{k}` ({n})" for k, n in result["detail_fields"].items()) or "none fetched",
           "\n## Sub-endpoints tried\n"]
    md += [f"- `calls/{{id}}/{s}`: {dict(v)}" for s, v in result["sub_endpoints"].items()]
    md += ["\n## Notes on the same leads\n", json.dumps(dict(note_hits)) or "{}"]
    (out_dir / f"calls_probe_{stamp}.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))

    if os.environ.get("GITHUB_ACTIONS"):
        def notice(title, msg):
            print(f"::notice title={title}::{msg.replace(chr(10), ' ')[:900]}")
        notice("Verdict", verdict)
        notice("Counts", f"calls {len(calls)}, 20s+ {len(long_calls)}, longest {result['max_duration_s']:.0f}s")
        notice("List fields", "; ".join(f"{k}={v['filled']}" for k, v in result["list_fields"].items()))
        notice("Detail fields", "; ".join(f"{k}={n}" for k, n in result["detail_fields"].items()) or "none")
        notice("Interesting detail", json.dumps([s.get("interesting", {}) for s in detail_samples[:3]], default=str))
        notice("Sub-endpoints", json.dumps(result["sub_endpoints"]))
        notice("Notes", json.dumps({"mentions": dict(note_hits), "examples": [
            {k: v for k, v in e.items() if k != "subject"} for e in note_examples]}))
    return result
