import csv
import sys
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fub import main  # noqa: E402
from fub_data.client import FUBClient, FUBError  # noqa: E402
from fub_data.stage_update import read_rows, run_stage_update  # noqa: E402

OLD, NEW = "Attempted Contact - Core Lead", "Resp to Text, Call Made - Not Connected"


class StageClient(FUBClient):
    def __init__(self, people):
        self.base_url = "https://fake"
        self.request_count = 0
        self.verbose = False
        self.people = people          # id -> stage
        self.puts = []

    def get(self, path, params=None):
        if path == "stages":
            names = ["Lead", OLD, NEW, "Trash"]
            return {"_metadata": {"collection": "stages"}, "stages": [{"id": i, "name": n} for i, n in enumerate(names)]}
        pid = int(path.split("/")[1])
        if pid not in self.people:
            raise FUBError(f"GET {path} -> 404: not found")
        return {"id": pid, "name": f"Lead {pid}", "stage": self.people[pid]}

    def put(self, path, body, params=None):
        pid = int(path.split("/")[1])
        self.puts.append((pid, body))
        self.people[pid] = body["stage"]
        return {"id": pid, "stage": body["stage"]}


def make_sheet(path):
    """Same layout as the real export: title block, then header on row 5."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Move to Resp to Text"
    ws.append(['Leads to move to "Resp to Text..."'])
    ws.append(["Bonus FUB person IDs"])
    ws.append(["Leads:", "=COUNTA(A6:A185)"])
    ws.append([])
    ws.append(["Bonus FUB ID", "Name", "Current stage in Bonus FUB", "Change to", "Group", "Done?"])
    ws.append([19, "A", OLD, NEW.lower(), "Yellow", None])   # target case differs -> normalised
    ws.append([20, "B", OLD, NEW, "Orange", None])           # already moved
    ws.append([21, "C", OLD, NEW, "Orange", None])           # moved elsewhere since export
    ws.append([22, "D", OLD, NEW, "Yellow", "Yes"])          # marked done
    ws.append([23, "E", OLD, NEW, "Yellow", None])           # not in FUB
    ws.append([19, "A", OLD, NEW, "Yellow", None])           # duplicate
    ws.append(["abc", "F", OLD, NEW, "Yellow", None])        # bad id
    wb.save(path)
    return path


def people():
    return {19: OLD, 20: NEW, 21: "Trash", 22: OLD}


def results(out):
    return {r["sheet_row"]: r for r in csv.DictReader(open(out))}


def test_read_rows_finds_header_below_title(tmp_path):
    rows = read_rows(make_sheet(tmp_path / "s.xlsx"))
    assert [r.fub_id for r in rows] == [19, 20, 21, 22, 23, 19, None]
    assert rows[0].expected == OLD and rows[3].done


def test_dry_run_writes_nothing(tmp_path):
    c = StageClient(people())
    res = run_stage_update(c, make_sheet(tmp_path / "s.xlsx"), tmp_path / "out")
    assert c.puts == []
    assert res["counts"] == {"would_update": 1, "already_set": 1, "skipped": 2,
                             "not_found": 1, "duplicate": 1, "error": 1}


def test_apply(tmp_path):
    c = StageClient(people())
    res = run_stage_update(c, make_sheet(tmp_path / "s.xlsx"), tmp_path / "out", apply=True)
    assert c.puts == [(19, {"stage": NEW})]            # exact FUB spelling, once
    rows = results(res["output"])
    assert rows["6"]["result"] == "updated" and rows["6"]["stage_before"] == OLD
    assert rows["8"]["result"] == "skipped" and "Trash" in rows["8"]["detail"]
    # re-run is a no-op
    res2 = run_stage_update(c, tmp_path / "s.xlsx", tmp_path / "out", apply=True)
    assert len(c.puts) == 1 and res2["counts"]["already_set"] == 2


def test_force_moves_changed_leads(tmp_path):
    c = StageClient(people())
    run_stage_update(c, make_sheet(tmp_path / "s.xlsx"), tmp_path / "out", apply=True, force=True)
    assert {pid for pid, _ in c.puts} == {19, 21}


def test_unknown_stage_aborts_before_any_write(tmp_path):
    c = StageClient(people())
    with pytest.raises(FUBError, match="not in this FUB account"):
        run_stage_update(c, make_sheet(tmp_path / "s.xlsx"), tmp_path / "out", stage="Nope", apply=True)
    assert c.puts == []


def test_csv_with_stage_flag(tmp_path):
    p = tmp_path / "ids.csv"
    p.write_text("fub_id\n19\n22\n")
    c = StageClient(people())
    res = run_stage_update(c, p, tmp_path / "out", stage="Lead", apply=True)
    assert res["counts"] == {"updated": 2}


def test_cli_requires_target(tmp_path, monkeypatch):
    p = tmp_path / "ids.csv"
    p.write_text("fub_id\n19\n")
    monkeypatch.setenv("FUB_API_KEY", "x")
    assert main(["set-stage", str(p)]) == 1


def test_pasted_ids_with_expected_stage(tmp_path):
    c = StageClient(people())
    res = run_stage_update(c, None, tmp_path / "out", ids="19, 20\n21 99", stage=NEW,
                           expect_stage=OLD, apply=True)
    assert c.puts == [(19, {"stage": NEW})]
    assert res["counts"] == {"updated": 1, "already_set": 1, "skipped": 1, "not_found": 1}


def test_brief_csv_lists_only_moved_leads(tmp_path):
    c = StageClient(people())
    res = run_stage_update(c, make_sheet(tmp_path / "s.xlsx"), tmp_path / "out", apply=True)
    rows = list(csv.DictReader(open(res["brief"])))
    assert rows == [{"FUB ID": "19", "Lead": "A", "New stage": NEW}]
    assert "moved" in Path(res["brief"]).name
