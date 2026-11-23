from datetime import date, timedelta

import screens
from test_screens import _figure_plate


def test_week_runs_monday_to_sunday_and_turns_over_on_monday():
    sunday, monday = date(2026, 11, 22), date(2026, 11, 23)
    week = screens._week(sunday)
    assert (week[0], week[-1]) == (date(2026, 11, 16), sunday)
    assert screens._week(monday)[0] == monday


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
    # The daily layout is landscape only; portrait shows the print whole and
    # ignores it (DECISIONS §30).
    dash, day = screens.dashboard(1200, 1600), screens.daily(1600, 1200)
    tall = screens.daily(1200, 1600)
    state.update_config(image_placements={"File:Fox.tif": {"daily": {"x": 0.2, "y": 0, "scale": 1.3}}})
    assert screens.dashboard(1200, 1600).tobytes() == dash.tobytes()
    assert screens.daily(1600, 1200).tobytes() != day.tobytes()
    assert screens.daily(1200, 1600).tobytes() == tall.tobytes()
    state.update_config(image_placements={"File:Fox.tif": {
        "hero": {"x": 0.2, "y": 0, "scale": 1.3}, "landscape": {"x": 0.2, "y": 0, "scale": 1.3}}})
    assert screens.daily(1600, 1200).tobytes() == day.tobytes()


def test_daily_renders_without_a_print(data_dir, fake_data):
    planner = {"events": [{"date": "2026-11-16", "time": "09:00", "owner": "m",
                           "title": "Short"}], "todos": {}}
    missing = {**fake_data["animal"], "path": str(data_dir / "gone.jpg")}
    for animal in (None, missing):
        img, _ = screens._daily(1200, 1600, animal, planner, date(2026, 11, 16))
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
    img, _ = screens._daily(1200, 1600, None, planner, date(2026, 11, 18))
    x = screens.DAILY_MARGIN + 4
    # Date number and weekday label per day: if a date runs into the previous
    # day's weekday label, two runs merge and the count drops.
    assert len(_ink_runs(img, x, x + 70)) == 14


def test_daily_print_is_full_bleed_and_fades_only_on_its_inner_edge():
    """Landscape: a scene covers the daily box at full opacity right up to
    the frame's top, right and bottom; only the inner left edge fades into
    the paper."""
    import tempfile
    from PIL import Image
    with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
        Image.new("RGB", (900, 1200), (20, 20, 20)).save(f.name)
        img, _ = screens._daily(1600, 1200, {"path": f.name, "common_name": "x"},
                                {"events": [], "todos": {}}, date(2026, 11, 16))
    x0 = screens._daily_art_box(1600, 1200)[0]
    for xy in ((1599, 600), (1300, 1199), (1599, 1199), (1599, 700), (1300, 1180)):
        assert img.getpixel(xy)[0] < 40, xy                  # opaque at the border
    assert img.getpixel((x0, 800)) == (255, 255, 255)        # transparent at the inner edge
    assert 40 < img.getpixel((x0 + 40, 800))[0] < 255         # fading in across it


def test_daily_figure_covers_its_box():
    """Landscape: full bleed applies to a figure on paper too: it is drawn
    over the whole box, not contained in it as the dashboard does."""
    import tempfile
    from PIL import Image
    box = screens._daily_art_box(1600, 1200)
    with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
        _figure_plate().save(f.name)
        art = screens._draw_art(Image.new("RGB", (1600, 1200), "white"), {"path": f.name},
                                box, lambda v: v, lambda v: v, mode="daily", layout="daily")
    assert art.rect == box


def _week_planner(monday):
    return {"events": [{"date": (monday + timedelta(days=i)).isoformat(), "time": "11:00",
                        "all_day": False, "owner": "m", "title": "Vet"} for i in range(7)],
            "todos": {}}


def test_days_before_today_are_faded_and_the_rest_full_strength():
    monday = date(2026, 11, 16)
    img, _ = screens._daily(1200, 1600, None, _week_planner(monday), monday + timedelta(days=3))
    x = screens.DAILY_MARGIN + 4
    numerals = _ink_runs(img, x, x + 70)[::2]            # date number, weekday, ...
    assert len(numerals) == 7
    darkest = [min(img.crop((x, y0, x + 70, y1)).convert("L").getdata()) for y0, y1 in numerals]
    assert all(v > 60 for v in darkest[:3]), darkest     # Mon-Wed faded
    assert all(v < 20 for v in darkest[3:]), darkest     # Thursday on at full strength


def test_date_number_top_is_level_with_the_first_events_capitals():
    monday = date(2026, 11, 16)
    img, _ = screens._daily(1200, 1600, None, _week_planner(monday), monday)
    x = screens.DAILY_MARGIN + 4
    numeral = _ink_runs(img, x, x + 70)[0]
    time_top = _ink_runs(img, x + 84, x + 150)[0][0]     # "11:00" in the time column
    assert abs(numeral[0] - time_top) <= 1


def test_polaroid_hangs_between_caption_and_list_and_draws_over_neither(monkeypatch, tmp_path):
    """Portrait: whatever the plate's shape, the card sits below the caption
    and above the todo list, and nothing of the card, its shadow or the
    twine lands on the caption, the list or the calendar: the page drawn
    with the polaroid differs from it drawn without only on paper."""
    from PIL import Image, ImageChops, ImageDraw
    planner = {"events": [{"date": "2026-11-16", "time": "09:00", "owner": "m",
                           "title": "A long title that wraps onto a second line"}],
               "todos": {"open": [{"owner": o, "title": "Something to do"}
                                  for o in ("m", "k", "mk") * 2],
                         "done": [{"owner": "m", "title": "Done", "date": "2026-11-15"}] * 3}}
    real = screens._draw_polaroid
    for name, size, ink in (("tall", (500, 1800), (180, 200, 320, 1600)),
                            ("wide", (2400, 600), (150, 220, 2250, 420))):
        sheet = Image.new("RGB", size, (238, 234, 224))
        ImageDraw.Draw(sheet).rectangle(ink, fill=(30, 30, 30))
        path = tmp_path / f"{name}.jpg"
        sheet.save(path)
        animal = {"path": str(path), "common_name": "fox", "sci_name": "Vulpes vulpes",
                  "native": "the Northern Hemisphere", "year": "1864", "place": "Amsterdam"}
        monkeypatch.setattr(screens, "_draw_polaroid", real)
        img, art = screens._daily(1200, 1600, animal, planner, date(2026, 11, 16))
        monkeypatch.setattr(screens, "_draw_polaroid", lambda *a: (0, 0, 0, 0))
        bare, _ = screens._daily(1200, 1600, animal, planner, date(2026, 11, 16))

        drawn = ImageChops.difference(img, bare).convert("L").point(lambda v: 255 if v else 0)
        page_ink = bare.convert("L").point(lambda v: 255 if v < 255 else 0)
        assert ImageChops.multiply(drawn, page_ink).getbbox() is None, name
        assert drawn.getbbox()[0] >= screens.DAILY_CAL_RIGHT, name

        right_ink = page_ink.crop((600, 0, 1200, 1600))
        caption_bottom = right_ink.crop((0, 0, 600, 600)).getbbox()[3]
        list_top = 600 + right_ink.crop((0, 600, 600, 1600)).getbbox()[1]
        x0, y0, x1, y1 = art.rect
        assert caption_bottom < y0 and y1 < list_top and 600 < x0 and x1 <= 1200, (name, art.rect)
        # Shown at a real size, limited by the photo's width or the room's height.
        assert (x1 - x0 >= 500) if name == "wide" else (y1 - y0 >= 600), (name, art.rect)
