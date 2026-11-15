import datetime, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import binremind as b


def test_short_names():
    assert b.short("Non-recyclable waste collection") == "non-recyclable"
    assert b.short("Paper and card collection") == "paper & card"
    assert b.short("Garden Waste collection") == "garden"
    assert b.short("Food waste collection") == "food"
    assert b.short("Mixed recycling collection") == "mixed recycling"


def test_message():
    m = b.message(datetime.date(2026, 11, 17), ["Food waste collection", "Paper and card collection"])
    assert m == "Bins tomorrow (Tue 17 Nov): food, paper & card"
