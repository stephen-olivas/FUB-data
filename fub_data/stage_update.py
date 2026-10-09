"""Bulk-move FUB people to a new stage from a spreadsheet.

Input is an .xlsx or .csv with one lead per row. The header row can sit below a
title block; it is found by looking for an ID column. Recognised columns
(case-insensitive):

  ID        "Bonus FUB ID", "FUB ID", "fub_id", "Person ID", "ID"     (required)
  Target    "Change to", "New stage", "Target stage"                  (or pass --stage)
  Expected  "Current stage ..."   if present, a lead whose stage no longer
                                  matches is skipped (someone already moved it)
  Done      "Done?"               rows marked yes / x / true are skipped
  Name      "Name"                only used in the results file

Per lead: read its current stage, skip it if already at the target or if it has
moved since the sheet was made, otherwise PUT the new stage. Dry run unless
apply=True. Every row's outcome goes to output/stage_update_<time>.csv, so a
re-run after a partial failure only touches what's left.
"""

from __future__ import annotations

import csv
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .client import FUBClient, FUBError

ID_HEADERS = {"bonus fub id", "fub id", "fub_id", "fub person id", "person id", "personid", "id"}
TARGET_HEADERS = {"change to", "new stage", "target stage", "move to", "stage to"}
DONE_VALUES = {"y", "yes", "x", "true", "done", "1"}


@dataclass
class Row:
    line: int                  # spreadsheet row number, for error messages
    fub_id: int | None
    name: str = ""
    target: str = ""
    expected: str = ""
    done: bool = False
    raw_id: str = ""


def _norm(v) -> str:
    return str(v).strip() if v is not None else ""


def _read_table(path: Path, sheet: str | None) -> list[list]:
    if path.suffix.lower() in {".xlsx", ".xlsm"}:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[sheet] if sheet else wb.worksheets[0]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    with path.open(newline="", encoding="utf-8-sig") as f:
        return [list(r) for r in csv.reader(f)]


def read_rows(path: str | Path, sheet: str | None = None, stage: str | None = None) -> list[Row]:
    path = Path(path)
    table = _read_table(path, sheet)

    header_idx = None
    for i, r in enumerate(table[:30]):
        if any(_norm(c).lower() in ID_HEADERS for c in r):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(f"{path.name}: no ID column found (looked for {sorted(ID_HEADERS)}).")

    header = [_norm(c).lower() for c in table[header_idx]]

    def col(pred) -> int | None:
        return next((j for j, h in enumerate(header) if h and pred(h)), None)

    c_id = col(lambda h: h in ID_HEADERS)
    c_target = col(lambda h: h in TARGET_HEADERS)
    c_expected = col(lambda h: h.startswith("current stage"))
    c_done = col(lambda h: h.rstrip("?") == "done")
    c_name = col(lambda h: h in {"name", "lead name", "full name"})

    if c_target is None and not stage:
        raise ValueError(f"{path.name}: no target-stage column ({sorted(TARGET_HEADERS)}); pass --stage.")

    def cell(r, j):
        return _norm(r[j]) if j is not None and j < len(r) else ""

    rows: list[Row] = []
    for i, r in enumerate(table[header_idx + 1:], start=header_idx + 2):
        raw = cell(r, c_id)
        if not raw:
            continue
        try:
            fid = int(float(raw))
        except ValueError:
            fid = None
        rows.append(Row(
            line=i, fub_id=fid, raw_id=raw, name=cell(r, c_name),
            target=stage or cell(r, c_target),
            expected=cell(r, c_expected),
            done=cell(r, c_done).lower() in DONE_VALUES,
        ))
    return rows


def rows_from_ids(ids: str, stage: str) -> list[Row]:
    """Rows from a pasted list of IDs (commas, spaces or newlines), e.g. a workflow input."""
    rows = []
    for n, raw in enumerate((t for t in re.split(r"[\s,;]+", ids) if t), 1):
        try:
            fid = int(float(raw))
        except ValueError:
            fid = None
        rows.append(Row(line=n, fub_id=fid, raw_id=raw, target=stage))
    return rows


def resolve_stages(client: FUBClient, wanted: set[str]) -> dict[str, str]:
    """Map each requested stage name to FUB's exact spelling; raise if any is unknown."""
    names = [s.get("name", "") for s in client.list_all("stages", "stages")]
    by_lower = {n.lower(): n for n in names}
    missing = sorted(w for w in wanted if w.lower() not in by_lower)
    if missing:
        raise FUBError(
            "Stage(s) not in this FUB account: " + ", ".join(repr(m) for m in missing)
            + "\nAvailable stages: " + ", ".join(names)
        )
    return {w: by_lower[w.lower()] for w in wanted}


def run_stage_update(
    client: FUBClient,
    path: str | Path | None,
    out_dir: Path,
    sheet: str | None = None,
    stage: str | None = None,
    apply: bool = False,
    force: bool = False,
    limit: int | None = None,
    ids: str | None = None,
    expect_stage: str | None = None,
) -> dict:
    if ids:
        if not stage:
            raise ValueError("--ids needs --stage.")
        rows = rows_from_ids(ids, stage)
        source = f"{len(rows)} pasted IDs"
    elif path:
        rows = read_rows(path, sheet=sheet, stage=stage)
        source = Path(path).name
    else:
        raise ValueError("Give a spreadsheet or --ids.")
    if expect_stage:
        for r in rows:
            r.expected = expect_stage
    if limit:
        rows = rows[:limit]

    stage_map = resolve_stages(client, {r.target for r in rows if r.target and r.fub_id})
    app_url = (os.environ.get("FUB_APP_URL") or "").rstrip("/")

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"stage_update_{'applied' if apply else 'dryrun'}_{stamp}.csv"

    mode = "APPLYING" if apply else "DRY RUN (no changes; add --apply to write)"
    print(f"{mode}: {len(rows)} rows from {source}", file=sys.stderr)

    counts: dict[str, int] = {}
    seen: set[int] = set()
    moved: list[list] = []   # brief list for sharing: leads that moved (or would, in a dry run)
    with out_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sheet_row", "fub_id", "name", "link", "stage_before", "target_stage", "result", "detail"])

        for n, r in enumerate(rows, 1):
            target = stage_map.get(r.target, r.target)
            before, result, detail, name = "", "", "", r.name

            if r.fub_id is None:
                result, detail = "error", f"not a FUB id: {r.raw_id!r}"
            elif r.fub_id in seen:
                result = "duplicate"
            elif r.done:
                result, detail = "skipped", "marked done in sheet"
            elif not target:
                result, detail = "error", "no target stage"
            else:
                seen.add(r.fub_id)
                try:
                    p = client.get(f"people/{r.fub_id}", {"fields": "id,name,stage"})
                    before = p.get("stage") or ""
                    name = name or p.get("name") or ""
                    if before.lower() == target.lower():
                        result = "already_set"
                    elif r.expected and before.lower() != r.expected.lower() and not force:
                        result = "skipped"
                        detail = f"stage is now {before!r}, sheet expected {r.expected!r} (use --force to move anyway)"
                    elif not apply:
                        result = "would_update"
                    else:
                        resp = client.put(f"people/{r.fub_id}", {"stage": target})
                        after = resp.get("stage")
                        if after and after.lower() != target.lower():
                            result, detail = "error", f"FUB returned stage {after!r}"
                        else:
                            result = "updated"
                except FUBError as exc:
                    msg = str(exc)
                    result = "not_found" if "-> 404" in msg else "error"
                    detail = msg[:300]

            counts[result] = counts.get(result, 0) + 1
            link = f"{app_url}/2/people/view/{r.fub_id}" if app_url and r.fub_id else ""
            if result in {"updated", "would_update"}:
                moved.append([r.fub_id, name, target, link])
            w.writerow([r.line, r.fub_id or r.raw_id, name, link, before, target, result, detail])
            f.flush()
            if result in {"error", "not_found", "skipped"}:
                print(f"  row {r.line} id {r.fub_id or r.raw_id}: {result} {detail}", file=sys.stderr)
            if n % 25 == 0:
                print(f"  {n}/{len(rows)} ...", file=sys.stderr)

    print("Results: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())), file=sys.stderr)
    brief_path = out_dir / f"stage_update_{'moved' if apply else 'would_move'}_{stamp}.csv"
    with brief_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["FUB ID", "Lead", "New stage"] + (["Link"] if app_url else []))
        for fid, name, target, link in moved:
            w.writerow([fid, name, target] + ([link] if app_url else []))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as sf:
            sf.write(f"## Stage update: {'applied' if apply else 'dry run (nothing changed)'}\n\n")
            sf.write(f"{len(rows)} leads from {source}\n\n| Result | Leads |\n|---|---:|\n")
            for k, v in sorted(counts.items()):
                sf.write(f"| {k} | {v} |\n")
            sf.write(f"\nIn the run's artifact: `{brief_path.name}` (ID, lead, new stage; one row per "
                     f"lead {'moved' if apply else 'that would move'}) and `{out_path.name}` (every row, with skips and errors).\n")

    print(f"Wrote {out_path}", file=sys.stderr)
    print(f"Wrote {brief_path} ({len(moved)} leads)", file=sys.stderr)
    return {"counts": counts, "output": str(out_path), "brief": str(brief_path), "applied": apply}
