"""In-memory stand-in for the FUB API, used by the tests."""

from __future__ import annotations

import base64
import json

from fub_data.client import FUBClient, FUBError


def person(pid, created, **kw):
    return {"id": pid, "created": created, "name": f"Lead {pid}", "stage": kw.pop("stage", "Lead"),
            "source": kw.pop("source", "Bonus Website"), "tags": kw.pop("tags", []),
            "contacted": kw.pop("contacted", True),
            "addresses": kw.pop("addresses", []), **kw}


PEOPLE = [  # newest first, like the API
    person(111, "2026-10-05T20:00:00Z"),                                                              # Oct 5 -> after window
    person(110, "2026-10-04T22:00:00Z", addresses=[{"type": "home", "code": "85003"}]),            # qualified (Phoenix)
    person(109, "2026-10-04T18:00:00Z", tags=["Short Sale"], addresses=[{"code": "30002"}]),         # unqualified tag
    person(108, "2026-10-03T18:00:00Z", addresses=[{"code": "59001"}]),                               # rural
    person(107, "2026-10-02T18:00:00Z", addresses=[{"code": "89002"}]),                               # opp via inbound text
    person(106, "2026-10-01T18:00:00Z", addresses=[{"code": "80002"}]),                               # outbound text only -> qualified
    person(105, "2026-09-30T18:00:00Z", stage="Appt Set", addresses=[{"code": "80002"}]),             # opp via stage
    person(104, "2026-09-29T18:00:00Z"),                                                              # licensed agent via note, no zip
    person(103, "2026-09-28T08:30:00Z", addresses=[{"code": "85004"}], contacted=False),              # first day of window (PT)
    person(102, "2026-09-28T03:00:00Z", addresses=[{"code": "85004"}]),                               # Sep 27 PT -> outside
    person(101, "2026-09-20T18:00:00Z"),
    person(100, "2026-09-19T18:00:00Z"),
]

ACTIVITY = {
    "textMessages": {
        107: [{"id": 1, "isIncoming": True, "message": "Yes, I would like to intro with an agent next week"}],
        106: [{"id": 2, "isIncoming": False, "message": "Would you like to speak with an agent?"}],
    },
    "notes": {104: [{"id": 3, "subject": "Call", "body": "Turns out lead is an agent in Mesa."}]},
    "events": {110: [{"id": 4, "type": "Seller Inquiry", "property": {"code": "85003", "type": "Single Family"}}]},
}


class FakeClient(FUBClient):
    def __init__(self, page_size=3):
        self.base_url = "https://fake"
        self.request_count = 0
        self.verbose = False
        self.page_size = page_size

    def get(self, path, params=None):
        self.request_count += 1
        params = dict(params or {})
        if path.startswith("https://fake/"):
            path, q = path[len("https://fake/"):].split("?", 1)
            params = json.loads(base64.b64decode(q))
        if path == "people":
            off = int(params.get("next") or 0)
            lim = min(int(params.get("limit", 10)), self.page_size)
            chunk = PEOPLE[off:off + lim]
            meta = {"collection": "people", "total": len(PEOPLE)}
            if off + lim < len(PEOPLE):
                nxt = {**params, "next": off + lim}
                meta["next"] = str(off + lim)
                meta["nextLink"] = "https://fake/people?" + base64.b64encode(json.dumps(nxt).encode()).decode()
            return {"_metadata": meta, "people": chunk}
        if path == "stages":
            return {"_metadata": {"collection": "stages"}, "stages": [{"id": 1, "name": "Lead"}, {"id": 2, "name": "Appt Set"}]}
        if path == "customFields":
            return {"_metadata": {"collection": "customfields"}, "customfields": [{"name": "customPropertyType", "label": "Property Type", "type": "dropdown", "choices": ["SFH", "Land"]}]}
        if path == "users":
            return {"_metadata": {"collection": "users"}, "users": [{"id": 1}]}
        if path == "deals":
            raise FUBError("GET deals -> 403: forbidden")
        coll = {"textMessages": "textmessages"}.get(path, path)
        recs = ACTIVITY.get(path, {}).get(params.get("personId"), [])
        return {"_metadata": {"collection": coll}, coll: recs}
