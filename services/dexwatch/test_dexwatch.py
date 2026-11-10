"""Tests for dexwatch's rule-enforcing half: parse() and apply(). No Google, no claude."""
import datetime, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dexwatch as d

NOW = datetime.datetime(2026, 11, 5, 12, 0, tzinfo=datetime.timezone.utc)
MSG = {"id": "m1", "subject": "Viewing confirmation", "date": "Thu, 5 Nov 2026", "link": "L"}


class Call:
    def __init__(self, fn): self.fn = fn
    def execute(self): return self.fn()


class FakeEvents:
    def __init__(self): self.log = []
    def insert(self, calendarId, body):
        self.log.append(("insert", body)); return Call(lambda: dict(body, id="new%d" % len(self.log)))
    def patch(self, calendarId, eventId, body):
        self.log.append(("patch", eventId, body)); return Call(lambda: dict({"id": eventId}, **body))
    def delete(self, **k): raise AssertionError("dexwatch must never delete")


class FakeCal:
    def __init__(self): self.ev = FakeEvents()
    def events(self): return self.ev


def ev(i, start, title="House viewing: Ms A (Dexters)"):
    return {"id": i, "summary": title, "description": "",
            "start": {"dateTime": start}, "end": {"dateTime": start}}


def test_parse():
    assert d.parse('x {"viewing_related":true,"actions":[],"telegram":"hi"} y')["telegram"] == "hi"
    assert d.parse('{"viewing_related":"yes"}') is None
    assert d.parse("no json") is None
    assert d.parse('{"viewing_related":false}')["actions"] == []


def test_create_is_30_min_uk_time_and_tagged():
    c = FakeCal()
    done = d.apply(c, {"actions": [{"op": "create", "start": "2026-11-07T14:30", "viewer": "Ms A"}]}, [], MSG, NOW)
    (_, body), = c.ev.log
    assert body["start"]["dateTime"] == "2026-11-07T14:30:00+00:00"
    assert body["end"]["dateTime"] == "2026-11-07T15:00:00+00:00"
    assert body["extendedProperties"]["private"]["dexwatch"] == "viewing"
    assert body["colorId"] == "7"
    assert body["summary"] == "House viewing: Ms A (Dexters)"
    assert done == ["Calendar: added Sat 7 Nov 14:30, House viewing: Ms A (Dexters)"]


def test_summer_time_offset():
    c = FakeCal()
    d.apply(c, {"actions": [{"op": "create", "start": "2027-04-03T10:00"}]}, [], MSG, NOW)
    assert c.ev.log[0][1]["start"]["dateTime"] == "2027-04-03T10:00:00+01:00"


def test_create_twice_same_start_is_one_event():
    c = FakeCal()
    existing = [ev("e1", "2026-11-07T14:30:00Z")]
    done = d.apply(c, {"actions": [{"op": "create", "start": "2026-11-07T14:30", "viewer": "Ms A"}]}, existing, MSG, NOW)
    assert c.ev.log == [] and done == []


def test_cancel_prefixes_never_deletes_and_is_idempotent():
    c = FakeCal()
    done = d.apply(c, {"actions": [{"op": "cancel", "event_id": "e1"}]}, [ev("e1", "2026-11-07T14:30:00Z")], MSG, NOW)
    assert c.ev.log[0][2]["summary"] == "(canc) House viewing: Ms A (Dexters)"
    assert done[0].startswith("Calendar: cancelled")
    c2 = FakeCal()
    d.apply(c2, {"actions": [{"op": "cancel", "event_id": "e1"}]},
            [ev("e1", "2026-11-07T14:30:00Z", "(canc) House viewing: Ms A (Dexters)")], MSG, NOW)
    assert c2.ev.log == []


def test_update_moves_and_rebooking_drops_prefix():
    c = FakeCal()
    done = d.apply(c, {"actions": [{"op": "update", "event_id": "e1", "start": "2026-11-08T11:00"}]},
                   [ev("e1", "2026-11-07T14:30:00Z", "(canc) House viewing: Ms A (Dexters)")], MSG, NOW)
    p = c.ev.log[0][2]
    assert p["summary"] == "House viewing: Ms A (Dexters)"
    assert p["end"]["dateTime"] == "2026-11-08T11:30:00+00:00"
    assert "moved Sat 7 Nov 14:30 to Sun 8 Nov 11:00" in done[0]


def test_create_into_cancelled_slot_reinstates_it():
    c = FakeCal()
    d.apply(c, {"actions": [{"op": "create", "start": "2026-11-07T14:30", "viewer": "Ms A"}]},
            [ev("e1", "2026-11-07T14:30:00Z", "(canc) House viewing: Ms A (Dexters)")], MSG, NOW)
    assert c.ev.log[0][0] == "patch" and c.ev.log[0][2]["summary"] == "House viewing: Ms A (Dexters)"


def test_unknown_ids_and_absurd_dates_are_refused():
    c = FakeCal()
    done = d.apply(c, {"actions": [{"op": "cancel", "event_id": "nope"},
                                   {"op": "update", "event_id": "nope", "start": "2026-11-08T11:00"},
                                   {"op": "create", "start": "2019-01-01T10:00"},
                                   {"op": "create", "start": "garbage"},
                                   {"op": "delete", "event_id": "e1"}]},
                   [ev("e1", "2026-11-07T14:30:00Z")], MSG, NOW)
    assert c.ev.log == [] and done == []


def test_note():
    assert d.note({"telegram": ""}, [], MSG) is None
    assert d.note({"telegram": "Viewing Sat"}, ["Calendar: added x"], MSG) == "Dexters: Viewing Sat\nCalendar: added x"
