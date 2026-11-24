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


def _sheet(tmp_path, name, size, ink, fill=(30, 30, 30), paper=(238, 234, 224)):
    from PIL import Image, ImageDraw
    sheet = Image.new("RGB", size, paper)
    ImageDraw.Draw(sheet).rectangle(ink, fill=fill)
    path = tmp_path / f"{name}.jpg"
    sheet.save(path)
    return str(path)


POLAROID_PLANNER = {
    "events": [{"date": "2026-11-16", "time": "09:00", "owner": "m",
                "title": "A long title that wraps onto a second line"}],
    "todos": {"open": [{"owner": o, "title": "Something to do"} for o in ("m", "k", "mk") * 2],
              "done": [{"owner": "m", "title": "Done", "date": "2026-11-15"}] * 3}}


def test_polaroids_hang_between_caption_and_list_and_draw_over_neither(monkeypatch, tmp_path):
    """Portrait: whatever the print's and the photo's shapes, one card or
    two, the cards sit below the caption and above the todo list, and
    nothing of the cards, their shadows, writing or the twine lands on the
    caption, the list or the calendar: the page drawn with the polaroids
    differs from it drawn without only on paper. (Was the one-card test;
    the day's photo now hangs under the print, DECISIONS §32.)"""
    from PIL import ImageChops
    plates = {"tall": _sheet(tmp_path, "tall", (500, 1800), (180, 200, 320, 1600)),
              "wide": _sheet(tmp_path, "wide", (2400, 600), (150, 220, 2250, 420))}
    photos = {None: None,
              "tall": {"id": "a", "path": _sheet(tmp_path, "p-tall", (1440, 1920), (0, 0, 1440, 1920), (90, 120, 60)),
                       "place": "Brooklyn Bridge, New York, United States", "taken": "2026-08-19"},
              "wide": {"id": "b", "path": _sheet(tmp_path, "p-wide", (2560, 1440), (0, 0, 2560, 1440), (90, 120, 60)),
                       "place": None, "taken": "2026-08-19"}}
    real = screens._draw_polaroids
    for plate, path in plates.items():
        animal = {"path": path, "common_name": "fox", "sci_name": "Vulpes vulpes",
                  "native": "the Northern Hemisphere", "year": "1864", "place": "Amsterdam"}
        for shot, photo in photos.items():
            case = (plate, shot)
            monkeypatch.setattr(screens, "_draw_polaroids", real)
            img, art = screens._daily(1200, 1600, animal, POLAROID_PLANNER, date(2026, 11, 16), photo)
            monkeypatch.setattr(screens, "_draw_polaroids", lambda *a: (0, 0, 0, 0))
            bare, _ = screens._daily(1200, 1600, animal, POLAROID_PLANNER, date(2026, 11, 16), photo)

            drawn = ImageChops.difference(img, bare).convert("L").point(lambda v: 255 if v else 0)
            page_ink = bare.convert("L").point(lambda v: 255 if v < 255 else 0)
            assert ImageChops.multiply(drawn, page_ink).getbbox() is None, case
            assert drawn.getbbox()[0] >= screens.DAILY_CAL_RIGHT, case

            right_ink = page_ink.crop((600, 0, 1200, 1600))
            caption_bottom = right_ink.crop((0, 0, 600, 600)).getbbox()[3]
            list_top = 600 + right_ink.crop((0, 600, 600, 1600)).getbbox()[1]
            x0, y0, x1, y1 = art.rect
            assert caption_bottom < y0 and y1 < list_top and 600 < x0 and x1 <= 1200, (case, art.rect)
            if shot is None:
                # Shown at a real size, limited by the photo's width or the room's height.
                assert (x1 - x0 >= 500) if plate == "wide" else (y1 - y0 >= 600), (case, art.rect)


def test_no_photo_hangs_the_print_alone_and_no_print_hangs_the_photo_alone(tmp_path):
    """A photo that can't be read draws exactly the single-print page; with
    no print the photo still hangs, alone."""
    animal = {"path": _sheet(tmp_path, "plate", (2400, 600), (150, 220, 2250, 420)),
              "common_name": "fox"}
    photo = {"id": "a", "path": _sheet(tmp_path, "p", (1440, 1920), (0, 0, 1440, 1920), (90, 120, 60)),
             "place": "Portland, Oregon", "taken": "2026-08-19"}
    today = date(2026, 11, 16)
    alone, _ = screens._daily(1200, 1600, animal, POLAROID_PLANNER, today, None)
    gone = {**photo, "path": str(tmp_path / "gone.jpg")}
    assert screens._daily(1200, 1600, animal, POLAROID_PLANNER, today, gone)[0].tobytes() == alone.tobytes()
    img, art = screens._daily(1200, 1600, None, POLAROID_PLANNER, today, photo)
    assert any(g > r + 30 for _, (r, g, b) in img.crop(art.rect).getcolors(1 << 20))


def test_tucked_second_card_keeps_its_whole_photo_visible(monkeypatch, tmp_path):
    """When the pair is too tall the second card tucks under the first,
    which is drawn on top and may cover only its top border: the second
    card's photo shows as much with the first card hung over it as
    without, at both tilts."""
    # Both cards at full width, where the opposite tilts reach furthest.
    animal = {"path": _sheet(tmp_path, "plate", (2400, 600), (150, 220, 2250, 420)),
              "common_name": "fox"}
    red = _sheet(tmp_path, "red", (1440, 1920), (0, 0, 1440, 1920), (255, 0, 0), (255, 0, 0))
    photo = {"id": "a", "path": red, "place": "Portland, Oregon", "taken": "2026-08-19"}
    layouts, pair_layout = [], screens._pair_layout
    monkeypatch.setattr(screens, "_pair_layout", lambda *a: layouts.append(pair_layout(*a)) or layouts[-1])

    def red_shown(hang_first: bool) -> int:
        hang, calls = screens._hang, []

        def spy(img, card, *a):     # the second card is hung first, then the first over it
            calls.append(card)
            return hang(img, card, *a) if hang_first or len(calls) == 1 else (0, 0, 0, 0)
        monkeypatch.setattr(screens, "_hang", spy)
        img, _ = screens._daily(1200, 1600, animal, POLAROID_PLANNER, date(2026, 11, 16), photo)
        monkeypatch.setattr(screens, "_hang", hang)
        return sum(1 for r, g, b in img.getdata() if r > 150 and g < 60 and b < 60)

    alone, under = red_shown(False), red_shown(True)
    (_, joined), _ = layouts
    assert not joined                                        # this pair has to tuck
    assert alone - under < 40, (alone, under)                # antialiasing at most


def test_long_place_shrinks_to_the_smallest_size_then_drops_its_region():
    from PIL import Image, ImageDraw
    d = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    ident = lambda v: v
    place = "Brooklyn Bridge, New York"
    big, small = screens.POLAROID_HAND_SIZES
    w = d.textlength(place, font=screens._hand(small, screens.POLAROID_HAND_PLACE_W))
    assert screens._fit_place(d, place, w, 1, ident) == (place, small)
    assert screens._fit_place(d, place, w - 1, 1, ident) == ("Brooklyn Bridge", big)
