"""The review page's upcoming animals must be the ones that will actually
hang, in the order they will hang."""
from datetime import date

import pytest

import datasources
from test_datasources import PLATE

ANIMALS = [("fox", "Vulpes", "the Northern Hemisphere", "mammal"),
           ("hare", "Lepus", "Europe", "mammal"),
           ("lynx", "Lynx", "Europe", "mammal")]


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
    """Each day's get_animal, run fresh, is what upcoming_animals listed for
    that day: same slots (period and offset included), same selection."""
    start, days, period, offset = date(2026, 11, 21), 6, 2, 1
    hung = []
    for i in range(days):
        d = date.fromordinal(start.toordinal() + i)
        hung.append((d.isoformat(), datasources.get_animal(
            d, offset=offset, period_days=period)["file"]))
    for p in (data_dir / "cache").iterdir():   # forget everything
        p.unlink()

    # Before fetch_upcoming nothing is resolved yet: every slot is pending.
    cold = datasources.upcoming_animals(start, days, offset, period)
    assert all(it["pending"] and it["animal"] is None for it in cold)
    datasources.fetch_upcoming(start, days, offset, period)
    listed = datasources.upcoming_animals(start, days, offset, period)
    assert not any(it["pending"] for it in listed)
    assert [(d, it["animal"]["file"]) for it in listed for d in it["dates"]] == hung
    assert len(listed) == len({f for _, f in hung})   # one entry per slot
    # Fetching ahead never touches what the panel is showing.
    assert not (data_dir / "cache" / "animal.json").exists()
    assert not (data_dir / "cache" / datasources.ANIMAL_IMG).exists()


def test_only_a_reviewed_or_adjusted_upcoming_image_is_kept(data_dir, commons):
    """Its day takes the upcoming image rather than searching again, so
    what was reviewed hangs even if Commons (or the selection) has changed.
    An image nobody touched is resolved afresh, so later selection fixes
    apply to it; a vetoed one is never kept."""
    day = date(2026, 11, 22)
    datasources.fetch_upcoming(day, 1)
    ahead = datasources.upcoming_animals(day, 1)[0]["animal"]
    commons["n"] = 2
    assert datasources.get_animal(day)["file"] != ahead["file"]
    (data_dir / "cache" / "animal.json").unlink()
    shown = datasources.get_animal(day, keep={ahead["file"]})
    assert shown["file"] == ahead["file"]
    assert (data_dir / "cache" / datasources.ANIMAL_IMG).read_bytes() == PLATE
    assert datasources.get_animal(day, vetoed=[ahead["file"]],
                                  keep={ahead["file"]})["file"] != ahead["file"]


def test_vetoed_upcoming_image_is_replaced_and_passed_slots_pruned(data_dir, commons):
    day = date(2026, 11, 21)
    datasources.fetch_upcoming(day, 2)
    first = datasources.upcoming_animals(day, 2)
    vetoed = [first[1]["animal"]["file"]]
    datasources.fetch_upcoming(day, 2, vetoed=vetoed)
    again = datasources.upcoming_animals(day, 2, vetoed=vetoed)
    assert again[0]["animal"]["file"] == first[0]["animal"]["file"]
    assert again[1]["animal"]["file"] not in vetoed
    datasources.fetch_upcoming(date(2026, 11, 22), 1)
    left = {p.name for p in (data_dir / "cache" / "upcoming").iterdir()}
    assert left == {f"{first[1]['slot']}.json", f"{first[1]['slot']}.jpg"}
