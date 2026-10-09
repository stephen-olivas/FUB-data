import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fub_data.calls_probe import run_calls_probe  # noqa: E402
from fub_data.client import FUBClient, FUBError  # noqa: E402

CALLS = [
    {"id": 1, "personId": 7, "duration": 95, "isIncoming": False, "recordingUrl": "https://r/1",
     "note": "Talked about timeline", "transcript": "Agent: hi ... " * 50},
    {"id": 2, "personId": 8, "duration": 5, "isIncoming": True},
]


class CallsClient(FUBClient):
    def __init__(self):
        self.base_url, self.request_count, self.verbose = "https://fake", 0, False

        class NoNet:
            def get(self, *a, **k):
                raise OSError("no network in tests")
        self.session = NoNet()

    def get(self, path, params=None):
        if path == "calls":
            pid = (params or {}).get("personId")
            recs = [c for c in CALLS if pid is None or c["personId"] == pid]
            return {"_metadata": {"collection": "calls"}, "calls": recs}
        if path.startswith("calls/") and path.count("/") == 1:
            return next(c for c in CALLS if str(c["id"]) == path.split("/")[1])
        if path == "notes":
            return {"_metadata": {"collection": "notes"}, "notes": [{"subject": "Call summary", "body": "x"}]}
        raise FUBError(f"GET {path} -> 404: nope")


def test_probe_finds_transcript_field(tmp_path):
    r = run_calls_probe(CallsClient(), tmp_path)
    assert r["verdict"].startswith("TRANSCRIPT FIELD FOUND: transcript")
    assert r["calls_20s_plus"] == 1
    assert r["sub_endpoints"]["transcript"] == {"404": 1}
    assert r["notes_mentioning"]["summary"] == 1
    sample = r["detail_samples"][0]["interesting"]
    assert sample["transcript"].startswith("<text") and sample["note"].startswith("<text")
    assert json.loads(next(tmp_path.glob("*.json")).read_text())["calls_checked"] == 2


def test_probe_by_ids(tmp_path):
    r = run_calls_probe(CallsClient(), tmp_path, ids=[8])
    assert r["calls_checked"] == 1 and r["calls_20s_plus"] == 0


def test_probe_checks_recording_download(tmp_path, monkeypatch):
    import requests

    class Resp:
        status_code, headers, url = 200, {"Content-Type": "audio/mpeg", "Content-Length": "1024"}, "https://r/1"
        def close(self): pass

    c = CallsClient()
    c.session.get = lambda *a, **k: Resp()
    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp())
    r = run_calls_probe(c, tmp_path)
    assert r["recording_checks"][0]["with_api_key"]["type"] == "audio/mpeg"
    assert r["systems"] == {"(none)": 2}
