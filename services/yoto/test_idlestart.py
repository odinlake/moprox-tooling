import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import idlestart as I

H = 3600


def run(msgs, last_play=0.0):
    g = I.Guard(act=False)
    g.last_play = last_play
    return [w for topic, m, t in msgs if (w := g.feed(topic, m, t))]


def start(t0, ins, card="hsGyZ", ch="09", pos=222):
    return [("events", {"playbackStatus": "playing"}, t0),
            ("events", {"cardId": card, "chapterKey": ch, "position": pos, "playbackStatus": "playing"}, t0 + 2),
            ("status", {"status": {"cardInserted": ins, "activeCard": card}}, t0 + 3)]


def test_fires_after_8h_physical():
    assert len(run(start(9 * H, 1))) == 1


def test_not_before_8h():
    assert run(start(7.9 * H, 1)) == []


def test_remote_and_alarm_left_alone():
    assert run(start(9 * H, 2)) == [] and run(start(9 * H, 4)) == []


def test_other_card_left_alone():
    assert run(start(9 * H, 1, card="aHQIg")) == []


def test_already_at_start():
    assert run(start(9 * H, 1, ch="01", pos=3)) == []


def test_waits_for_late_card_report():
    msgs = [("events", {"playbackStatus": "playing"}, 9 * H),
            ("status", {"status": {"cardInserted": 1}}, 9 * H + 1),
            ("events", {"cardId": "hsGyZ", "chapterKey": "05", "position": 40}, 9 * H + 5)]
    assert len(run(msgs)) == 1


def test_stale_reports_do_not_count():
    g = I.Guard(act=False)
    g.last_play = 0.0
    g.feed("events", {"cardId": "hsGyZ", "chapterKey": "05", "position": 40}, 100)
    g.feed("status", {"status": {"cardInserted": 1}}, 100)
    assert g.feed("events", {"playbackStatus": "playing"}, 9 * H) is None   # needs this start's own reports
