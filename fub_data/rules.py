"""Qualification rules.

A lead is classified as:
  Unqualified  - any `unqualified` rule matches (hard stop)
  Opportunity  - no unqualified rule matches and any `opportunity` signal matches
  Qualified    - everything else

Rules live in config/criteria.yaml. Each rule has an `any_of` block; a rule
matches when any one of its matchers finds evidence. Every match records a
short evidence string so a human can audit the classification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .zips import extract_zips

TEXT_SOURCES = ("background", "notes", "texts", "events", "calls")
SKIP_KEYS = {"id", "personId", "created", "updated", "url", "sourceUrl", "picture", "userId",
             "createdById", "updatedById", "dealId", "pondId", "phone", "fromNumber", "toNumber"}


@dataclass
class LeadContext:
    person: dict
    activity: dict[str, list[dict]] = field(default_factory=dict)
    service_zips: dict[str, dict] = field(default_factory=dict)
    texts_inbound_only: bool = True

    # cached derived values
    _texts: dict[str, list[str]] | None = None

    def zips(self) -> list[tuple[str, str]]:
        return extract_zips(self.person, self.activity.get("events"))

    def text_blobs(self) -> dict[str, list[str]]:
        if self._texts is None:
            blobs: dict[str, list[str]] = {k: [] for k in TEXT_SOURCES}
            bg = self.person.get("background")
            if isinstance(bg, str) and bg.strip():
                blobs["background"].append(bg)
            for kind in ("notes", "events", "calls"):
                for rec in self.activity.get(kind, []) or []:
                    blobs[kind].append(_flatten_text(rec))
            for rec in self.activity.get("texts", []) or []:
                if self.texts_inbound_only and not _is_inbound(rec):
                    continue
                blobs["texts"].append(_flatten_text(rec))
            self._texts = blobs
        return self._texts


def _is_inbound(rec: dict) -> bool:
    if "isIncoming" in rec:
        return bool(rec["isIncoming"])
    direction = str(rec.get("direction") or rec.get("type") or "").lower()
    if direction:
        return direction.startswith("in")
    return True  # unknown direction -> keep (discovery report flags this)


def _flatten_text(obj: Any, key: str | None = None) -> str:
    if key in SKIP_KEYS:
        return ""
    if isinstance(obj, str):
        return "" if obj.startswith("http") else obj
    if isinstance(obj, dict):
        return " | ".join(t for k, v in obj.items() if (t := _flatten_text(v, k)))
    if isinstance(obj, list):
        return " | ".join(t for v in obj if (t := _flatten_text(v)))
    return ""


def _contains_any(value: str | None, needles: list[str]) -> str | None:
    if not value:
        return None
    low = value.lower()
    for n in needles:
        if n and n.lower() in low:
            return n
    return None


def _snippet(text: str, start: int, end: int, pad: int = 50) -> str:
    s = max(0, start - pad)
    e = min(len(text), end + pad)
    out = re.sub(r"\s+", " ", text[s:e]).strip()
    return ("…" if s else "") + out + ("…" if e < len(text) else "")


def _keyword_patterns(keywords: list[str]) -> list[tuple[str, re.Pattern]]:
    pats = []
    for kw in keywords or []:
        body = r"\s+".join(re.escape(w) for w in kw.split())
        pats.append((kw, re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)))
    return pats


def evaluate_matchers(spec: dict, ctx: LeadContext) -> list[str]:
    """Return evidence strings for every matcher in `spec` that fires."""
    p = ctx.person
    ev: list[str] = []

    wanted_tags = {t.lower() for t in spec.get("tags") or []}
    for tag in p.get("tags") or []:
        if str(tag).lower() in wanted_tags:
            ev.append(f"tag: {tag}")

    hit = _contains_any(p.get("source"), spec.get("sources") or [])
    if hit:
        ev.append(f"source: {p.get('source')}")

    stages = {s.lower() for s in spec.get("stages") or []}
    if p.get("stage") and p["stage"].lower() in stages:
        ev.append(f"stage: {p['stage']}")

    for fname, wanted in (spec.get("custom_fields") or {}).items():
        val = p.get(fname)
        if val in (None, "", []):
            continue
        sval = ", ".join(map(str, val)) if isinstance(val, list) else str(val)
        wanted = wanted if isinstance(wanted, list) else [wanted]
        if "*" in wanted or _contains_any(sval, [str(w) for w in wanted]):
            ev.append(f"{fname}: {sval}")

    ptypes = spec.get("property_types") or []
    if ptypes:
        for e in ctx.activity.get("events", []) or []:
            prop = e.get("property") or {}
            ptype = prop.get("type") if isinstance(prop, dict) else None
            if _contains_any(ptype, ptypes):
                ev.append(f"property type: {ptype}")
                break

    if spec.get("keywords"):
        sources = spec.get("search_in") or list(TEXT_SOURCES)
        blobs = ctx.text_blobs()
        for kw, pat in _keyword_patterns(spec["keywords"]):
            found = False
            for src in sources:
                for text in blobs.get(src, []):
                    m = pat.search(text)
                    if m:
                        ev.append(f'{src} "{kw}": {_snippet(text, m.start(), m.end())}')
                        found = True
                        break
                if found:
                    break

    if spec.get("zip_outside_service_area"):
        zips = ctx.zips()
        if zips:
            z, origin = zips[0]
            if z not in ctx.service_zips:
                ev.append(f"zip {z} ({origin}) not in service area")

    if spec.get("has_appointment") and ctx.activity.get("appointments"):
        ev.append(f"appointments: {len(ctx.activity['appointments'])}")
    if spec.get("has_deal") and ctx.activity.get("deals"):
        ev.append(f"deals: {len(ctx.activity['deals'])}")

    return ev


def required_activity(criteria: dict) -> list[str]:
    """Which per-person endpoints the configured rules need."""
    need: set[str] = set()
    specs = [r.get("any_of", {}) for r in criteria.get("unqualified", [])]
    specs.append(criteria.get("opportunity", {}).get("any_of", {}))
    for spec in specs:
        if spec.get("keywords"):
            need.update(s for s in (spec.get("search_in") or TEXT_SOURCES) if s != "background")
        if spec.get("property_types") or spec.get("zip_outside_service_area"):
            need.add("events")
        if spec.get("has_appointment"):
            need.add("appointments")
        if spec.get("has_deal"):
            need.add("deals")
    disabled = set((criteria.get("enrichment") or {}).get("skip", []) or [])
    return sorted(need - disabled)


@dataclass
class Classification:
    status: str
    unqualified_reasons: dict[str, list[str]]
    opportunity_evidence: list[str]
    review_flags: list[str]
    zip: str | None
    zip_origin: str | None
    market: str | None


def classify(ctx: LeadContext, criteria: dict) -> Classification:
    reasons: dict[str, list[str]] = {}
    for rule in criteria.get("unqualified", []):
        if rule.get("enabled", True) is False:
            continue
        evidence = evaluate_matchers(rule.get("any_of", {}), ctx)
        if evidence:
            reasons[rule["id"]] = evidence

    opp = evaluate_matchers(criteria.get("opportunity", {}).get("any_of", {}), ctx)

    flags: list[str] = []
    zips = ctx.zips()
    zip_, origin = (zips[0] if zips else (None, None))
    market = (ctx.service_zips.get(zip_) or {}).get("market") if zip_ else None
    if not zip_:
        flags.append("no_zip_found")

    if reasons:
        status = "Unqualified"
        if opp:
            flags.append("opportunity_signal_on_unqualified_lead")
    elif opp:
        status = "Opportunity"
    else:
        status = "Qualified"

    return Classification(status, reasons, opp, flags, zip_, origin, market)
