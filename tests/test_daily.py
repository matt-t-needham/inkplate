from datetime import date, timedelta

import screens
from test_screens import _figure_plate


def test_week_runs_monday_to_sunday_and_turns_over_on_monday():
    sunday, monday = date(2026, 11, 22), date(2026, 11, 23)
    week = screens._week(sunday)
    assert (week[0], week[-1]) == (date(2026, 11, 16), sunday)
    assert screens._week(monday)[0] == monday


def _bar_rows(img):
    """Rows of the frame where today's yellow bar is drawn (left of the dates)."""
    return [y for y in range(img.height) if img.getpixel((30, y)) == screens.YELLOW]


def test_only_todays_block_is_highlighted():
    monday = date(2026, 11, 16)
    planner = {"events": [{"date": (monday + timedelta(days=i)).isoformat(), "time": "09:00",
                           "all_day": False, "owner": "m", "title": "x"} for i in range(7)],
               "todos": {}}
    spans = []
    for today in (monday, monday + timedelta(days=6)):
        rows = _bar_rows(screens._daily(1200, 1600, None, planner, today))
        assert rows and rows[-1] - rows[0] + 1 == len(rows)   # one unbroken block
        assert len(rows) < 1600 // 7                         # ...and only one day's
        spans.append(rows)
    assert spans[0][-1] < spans[1][0]                        # Monday's above Sunday's


def test_list_takes_two_open_per_owner_then_three_newest_done():
    open_ = [{"owner": o, "title": f"{o}{i}"} for o in ("mk", "k", "m") for i in range(5)]
    done = [{"owner": "m", "title": f"d{i}", "date": f"2026-11-{10 + i:02d}"} for i in range(5)]
    rows = screens._todo_rows({"open": open_, "done": done})
    assert [(t["title"], d) for t, d in rows] == [
        ("m0", False), ("k0", False), ("mk0", False),
        ("m1", False), ("k1", False), ("mk1", False),
        ("d4", True), ("d3", True), ("d2", True)]


def test_daily_and_dashboard_placements_are_independent(data_dir, fake_data):
    import state
    _figure_plate().save(fake_data["animal"]["path"])
    fake_data["animal"]["file"] = "File:Fox.tif"
    state.update_config(portrait_layout="hero")
    dash, day = screens.dashboard(1200, 1600), screens.daily(1200, 1600)
    state.update_config(image_placements={"File:Fox.tif": {"daily": {"x": 0.2, "y": 0, "scale": 1.3}}})
    assert screens.dashboard(1200, 1600).tobytes() == dash.tobytes()
    assert screens.daily(1200, 1600).tobytes() != day.tobytes()
    state.update_config(image_placements={"File:Fox.tif": {
        "hero": {"x": 0.2, "y": 0, "scale": 1.3}, "landscape": {"x": 0.2, "y": 0, "scale": 1.3}}})
    assert screens.daily(1200, 1600).tobytes() == day.tobytes()


def test_daily_renders_without_a_print(data_dir, fake_data):
    planner = {"events": [{"date": "2026-11-16", "time": "09:00", "owner": "m",
                           "title": "Short"}], "todos": {}}
    missing = {**fake_data["animal"], "path": str(data_dir / "gone.jpg")}
    for animal in (None, missing):
        img = screens._daily(1200, 1600, animal, planner, date(2026, 11, 16))
        # the art's half is paper
        assert img.crop((600, 0, 1200, 1600)).getextrema() == ((255, 255),) * 3


def _ink_runs(img, x0, x1):
    """Vertical runs of rows with any ink between x0 and x1."""
    runs, start = [], None
    for y in range(img.height):
        inked = any(img.getpixel((x, y)) != (255, 255, 255) for x in range(x0, x1))
        if inked and start is None:
            start = y
        elif not inked and start is not None:
            runs.append((start, y))
            start = None
    return runs


def test_sparse_days_keep_their_date_column_clear_of_the_next_day():
    """A day with one event or none is still as tall as its date and
    weekday: the next day's date starts below the weekday label."""
    planner = {"events": [{"date": "2026-11-16", "time": "09:00", "owner": "m",
                           "title": "x"}], "todos": {}}
    img = screens._daily(1200, 1600, None, planner, date(2026, 11, 18))
    x = screens.DAILY_MARGIN + 4
    # Date number and weekday label per day: if a date runs into the previous
    # day's weekday label, two runs merge and the count drops.
    assert len(_ink_runs(img, x, x + 70)) == 14


def test_daily_print_is_full_bleed_and_fades_only_on_its_inner_edge():
    """A scene covers the daily box at full opacity right up to the frame's
    top, right and bottom; only the inner left edge fades into the paper."""
    import tempfile
    from PIL import Image
    with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
        Image.new("RGB", (900, 1200), (20, 20, 20)).save(f.name)
        img = screens._daily(1200, 1600, {"path": f.name, "common_name": "x"},
                             {"events": [], "todos": {}}, date(2026, 11, 16))
    for xy in ((1199, 800), (1000, 1599), (1199, 1599), (1199, 1000), (1000, 1580)):
        assert img.getpixel(xy)[0] < 40, xy                  # opaque at the border
    assert img.getpixel((600, 800)) == (255, 255, 255)       # transparent at the inner edge
    assert 40 < img.getpixel((640, 800))[0] < 255             # fading in across it


def test_daily_figure_covers_its_box():
    """Full bleed applies to a figure on paper too: it is drawn over the
    whole box, not contained in it as the dashboard does."""
    import tempfile
    from PIL import Image
    box = screens._daily_art_box(1200, 1600)
    with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
        _figure_plate().save(f.name)
        art = screens._draw_art(Image.new("RGB", (1200, 1600), "white"), {"path": f.name},
                                box, lambda v: v, lambda v: v, mode="daily", layout="daily")
    assert art.rect == box
