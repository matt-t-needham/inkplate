"""The review page's lookahead: upcoming prints must be the ones that will
actually hang, in the order they will hang."""
import io
import json
from datetime import date

import pytest
from PIL import Image, ImageDraw

import datasources

ANIMALS = [("fox", "Vulpes", "the Northern Hemisphere", "mammal"),
           ("hare", "Lepus", "Europe", "mammal"),
           ("lynx", "Lynx", "Europe", "mammal")]


def _plate() -> bytes:
    im = Image.new("RGB", (300, 400), (240, 235, 220))
    ImageDraw.Draw(im).ellipse([80, 100, 220, 320], fill=(40, 35, 30))
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return buf.getvalue()


PLATE = _plate()


@pytest.fixture()
def commons(monkeypatch):
    """A fake Commons: two IZ plates per genus; `edition` changes their
    titles, standing in for Commons changing between review and the day."""
    monkeypatch.setattr(datasources, "ANIMALS", ANIMALS)
    monkeypatch.setattr(datasources, "COLLECTIONS", [datasources.COLLECTIONS[0]])
    edition = {"n": 1}

    def fake_json(url, params=None, **k):
        genus = params["gsrsearch"].split()[2]
        return {"query": {"pages": {
            str(i): {"title": f"File:{genus} {genus.lower()} - Iconographia Zoologica "
                              f"- ed{edition['n']} - {i}.tif",
                     "imageinfo": [{"thumburl": f"https://thumb.example/{genus}{i}.jpg"}]}
            for i in (1, 2)}}}
    monkeypatch.setattr(datasources, "_get_json", fake_json)
    monkeypatch.setattr(datasources, "_get_bytes", lambda *a, **k: PLATE)
    return edition


def test_review_order_is_the_order_the_panel_will_hang(data_dir, commons):
    """Each day's get_animal, run fresh, is what the lookahead listed for that
    day: same slots (period and offset included), same selection."""
    start, days, period, offset = date(2026, 11, 21), 6, 2, 1
    hung = []
    for i in range(days):
        d = date.fromordinal(start.toordinal() + i)
        hung.append((d.isoformat(), datasources.get_animal(
            d, offset=offset, period_days=period)["file"]))
    for p in (data_dir / "cache").iterdir():   # forget everything
        p.unlink()

    listed = datasources.upcoming_animals(start, days, offset, period)
    assert [(d, it["animal"]["file"]) for it in listed for d in it["dates"]] == hung
    assert len(listed) == len({f for _, f in hung})   # one entry per slot
    # Looking ahead never touches what the panel is showing.
    assert not (data_dir / "cache" / "animal.json").exists()
    assert not (data_dir / "cache" / datasources.ANIMAL_IMG).exists()


def test_a_reviewed_print_hangs_even_if_commons_changes(data_dir, commons):
    """Its day takes the looked-ahead print rather than searching again."""
    day = date(2026, 11, 22)
    ahead = datasources.upcoming_animals(day, 1)[0]["animal"]
    commons["n"] = 2
    shown = datasources.get_animal(day)
    assert shown["file"] == ahead["file"]
    assert (data_dir / "cache" / datasources.ANIMAL_IMG).read_bytes() == PLATE
    # A vetoed one is not adopted: the day is searched again.
    assert datasources.get_animal(date(2026, 11, 22), vetoed=[ahead["file"]])["file"] \
        != ahead["file"]


def test_vetoed_lookahead_is_replaced_and_passed_slots_pruned(data_dir, commons):
    day = date(2026, 11, 21)
    first = datasources.upcoming_animals(day, 2)
    vetoed = [first[1]["animal"]["file"]]
    again = datasources.upcoming_animals(day, 2, vetoed=vetoed)
    assert again[0]["animal"]["file"] == first[0]["animal"]["file"]
    assert again[1]["animal"]["file"] not in vetoed
    datasources.upcoming_animals(date(2026, 11, 22), 1)
    left = {p.name for p in (data_dir / "cache" / "prints").iterdir()}
    assert left == {f"{first[1]['slot']}.json", f"{first[1]['slot']}.jpg"}
    assert json.loads((data_dir / "cache" / "prints"
                       / f"{first[1]['slot']}.json").read_text())["data"]["file"] \
        == again[1]["animal"]["file"]
