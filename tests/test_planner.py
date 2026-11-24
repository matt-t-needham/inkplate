"""The planner export → the planner screens (DECISIONS §31). Synthetic data."""

import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone

import pytest

import config
import planner_source

GENERATED = datetime(2026, 11, 24, 4, 0, tzinfo=timezone.utc)   # 20:00 on the 23rd, LA


def _export(**over) -> dict:
    cards = [{"id": cid, "title": cid.capitalize(),
              "squares": [{"row": i // 4, "col": i % 4, "summary": f"Square {i}",
                           "done": i < 3, "done_at": None} for i in range(16)]}
             for cid in ("matt", "kass", "together")]
    return {"schema_version": 1, "generated_at": GENERATED.isoformat(),
            "view": "daily", "bingo_card": "kass", "cards": cards,
            "next_up": {"matt": {"id": "t1", "text": "Sort the shed"}, "kass": None,
                        "together": {"id": "t2", "text": "Book a table"}},
            "completed": [], "calendar": []} | over


@pytest.fixture()
def export(tmp_path, monkeypatch):
    """Write an export where config points; returns the writer."""
    path = tmp_path / "inkplate.json"
    monkeypatch.setattr(config, "PLANNER_EXPORT", path)
    monkeypatch.setattr(planner_source, "_failures", 0)

    def write(**over):
        path.write_text(json.dumps(_export(**over)))
    return write


@pytest.fixture()
def los_angeles():
    # POSIX rule rather than a zone name: no tzdata needed in the image.
    old = os.environ.get("TZ")
    os.environ["TZ"] = "PST8PDT,M3.2.0,M11.1.0"
    time.tzset()
    yield
    if old is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_export_maps_to_the_daily_screen_with_local_done_dates(export, los_angeles):
    """Next-up items in M, K, MK order (a null owner skipped); a done_at
    after UTC midnight but before LA midnight is still the LA day before."""
    export(completed=[
        {"kind": "todo", "id": "t9", "owner": "together", "text": "Late one",
         "done_at": "2026-11-24T07:59:59.000000Z"},          # 23:59:59 on the 23rd, LA
        {"kind": "square", "id": "kass:0:0", "owner": "kass", "text": "Square 0",
         "done_at": "2026-11-24T08:00:00.000000Z", "row": 0, "col": 0}])   # 00:00 on the 24th
    p = planner_source.get_planner(date(2026, 11, 23), now=GENERATED)
    assert p["todos"]["open"] == [{"owner": "m", "title": "Sort the shed"},
                                  {"owner": "mk", "title": "Book a table"}]
    assert [(t["owner"], t["date"]) for t in p["todos"]["done"]] == [
        ("mk", "2026-11-23"), ("k", "2026-11-24")]
    assert (p["example"], p["example_events"], p["stale"]) == (False, True, None)


def test_offline_marker_from_exactly_twice_the_cadence(export, los_angeles):
    export()
    limit = GENERATED + timedelta(seconds=config.PLANNER_STALE_S)
    today = date(2026, 11, 23)
    assert planner_source.get_planner(today, now=limit - timedelta(seconds=1))["stale"] is None
    # The export's own time, not the render's: the same text every render.
    for later in (limit, limit + timedelta(hours=1)):
        assert planner_source.get_planner(today, now=later)["stale"] == \
            "Planner offline since 20:00"


def test_unusable_export_falls_back_to_the_labelled_stub_and_escalates(export, caplog):
    caplog.set_level(logging.WARNING, logger="inkplate.planner")
    today = date(2026, 11, 23)
    export(schema_version=2)
    levels = []
    for _ in range(3):
        p = planner_source.get_planner(today)
        assert p["example"] is True and p["stale"] is None
        levels.append(caplog.records[-1].levelname)
    assert levels == ["WARNING", "WARNING", "ERROR"]
    assert planner_source.get_bingo(today)["example"] is True
    # A wrong-typed value (unhashable, so a bare membership test would raise
    # TypeError) is the same logged fallback, and view() doesn't raise.
    export(bingo_card=["matt"])
    assert planner_source.get_bingo(today)["example"] is True
    assert planner_source.view() == "daily"


def test_planner_screen_follows_the_exports_view(data_dir, fake_data, export):
    """A flip of the view re-renders at the next wake, not the next slot,
    and the device is told to redraw."""
    import renderer
    import state
    export(view="daily")
    state.update_config(screen="planner")
    m1 = renderer.ensure_fresh()
    assert m1["drawn"] == "daily"
    assert renderer.ensure_fresh()["rendered_at"] == m1["rendered_at"]
    export(view="bingo")
    m2 = renderer.ensure_fresh()
    assert (m2["screen"], m2["drawn"]) == ("planner", "bingo")
    assert m2["image_version"] == m1["image_version"] + 1
