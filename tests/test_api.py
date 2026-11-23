import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import config
import main
import palette
import screens
import state


@pytest.fixture()
def client(data_dir, fake_data):
    # fake_data: the default screen is the dashboard, whose renders consult
    # external sources — stubbed so the suite stays offline.
    return TestClient(main.app, raise_server_exceptions=False)


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "inkplate" in r.text


def test_state_shape(client):
    st = client.get("/api/state").json()
    assert set(st) == {"config", "screens", "portrait_layouts", "art_layout", "render",
                       "last_checkin", "current", "battery_percent"}
    assert st["portrait_layouts"] == list(screens.PORTRAIT_LAYOUTS)
    assert st["portrait_layouts"][0] == screens.DEFAULT_PORTRAIT_LAYOUT == "hero"
    assert "placeholder" in st["screens"]
    assert st["last_checkin"] is None


def test_config_update_and_validation(client):
    r = client.post("/api/config", json={"refresh_minutes": 15, "screen": "alignment"})
    assert r.status_code == 200
    assert r.json()["refresh_minutes"] == 15
    assert client.post("/api/config", json={"screen": "bogus"}).status_code == 400
    assert client.post("/api/config", json={"unrelated": 1}).status_code == 400
    assert client.post("/api/config", json={"latitude": 91}).status_code == 400
    assert client.post("/api/config", json={"longitude": -200}).status_code == 400
    r = client.post("/api/config", json={"latitude": 51.5, "longitude": -0.12,
                                         "location_name": "London"})
    assert r.status_code == 200
    cfg = r.json()
    assert (cfg["latitude"], cfg["longitude"]) == (51.5, -0.12)
    assert cfg["location_name"] == "London"
    r = client.post("/api/config", json={"show_snow": False,
                                         "snow_location_name": "Timberline"})
    assert r.status_code == 200 and r.json()["show_snow"] is False
    assert client.post("/api/config", json={"snow_latitude": 95}).status_code == 400


def test_rotation_config_and_portrait_frame(client):
    assert client.post("/api/config", json={"rotation": 45}).status_code == 400
    assert client.post("/api/config", json={"portrait_layout": "sideways"}).status_code == 400
    r = client.post("/api/config", json={"rotation": 90, "portrait_layout": "hero"})
    assert r.status_code == 200
    cfg = r.json()
    assert (cfg["rotation"], cfg["portrait_layout"]) == (90, "hero")

    # The device frame is always the panel's native 1600x1200, whatever the
    # hanging; the previews are upright (portrait) so the pane shows the wall.
    r = client.get("/api/display.png")
    assert Image.open(io.BytesIO(r.content)).size == (1600, 1200)
    for mode in ("raw", "dithered", "panel"):
        r = client.get(f"/api/preview.png?mode={mode}")
        assert Image.open(io.BytesIO(r.content)).size == (1200, 1600), mode
    assert client.get("/api/state").json()["render"]["rotation"] == 90


def test_render_bumps_version_and_logs_event(client):
    v0 = state.load_config()["image_version"]
    r = client.post("/api/render")
    assert r.status_code == 200
    assert r.json()["image_version"] == v0 + 1
    kinds = [e["kind"] for e in client.get("/api/events").json()]
    assert "render" in kinds


def test_display_png_is_indexed_spectra(client):
    r = client.get("/api/display.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert int(r.headers["x-image-version"]) >= 1
    assert int(r.headers["x-sleep-seconds"]) > 0
    img = Image.open(io.BytesIO(r.content))
    assert img.mode == "P"
    assert img.size == (1600, 1200)
    used = set(img.convert("RGB").getdata())
    assert used.issubset(set(palette.DITHER_PALETTE))


def test_display_meta_tethered_switch(client):
    meta = client.get("/api/display/meta").json()
    assert meta["tethered"] is False
    # sleeps to the next wake slot: at most one cadence, never less than the grace
    assert config.WAKE_GRACE_S <= meta["sleep_seconds"] <= state.load_config()["refresh_minutes"] * 60 + config.WAKE_GRACE_S

    client.post("/api/config", json={"tethered": True, "tethered_poll_seconds": 20})
    meta = client.get("/api/display/meta").json()
    assert meta["tethered"] is True
    assert meta["sleep_seconds"] == 20


def test_preview_modes(client):
    for mode in ("raw", "dithered", "panel"):
        r = client.get(f"/api/preview.png?mode={mode}")
        assert r.status_code == 200, mode
        assert r.headers["content-type"] == "image/png"
    assert client.get("/api/preview.png?mode=x").status_code == 400


def test_checkin_flow(client):
    payload = {"battery_voltage": 3.91, "rssi": -61, "boot_count": 7,
               "shown_version": 1, "wake_reason": 4, "duration_ms": 9000,
               "fw_version": "0.1.0", "error": None}
    r = client.post("/api/device/checkin", json=payload)
    assert r.status_code == 200
    assert "sleep_seconds" in r.json()

    st = client.get("/api/state").json()
    assert st["last_checkin"]["battery_voltage"] == 3.91
    assert st["last_checkin"]["rssi"] == -61
    assert st["battery_percent"] == 71


def test_checkin_error_creates_event(client, caplog):
    import logging
    caplog.set_level(logging.INFO)
    client.post("/api/device/checkin", json={"boot_count": 1, "error": "wifi timeout"})
    events = client.get("/api/events").json()
    assert any(e["kind"] == "device_error" and "wifi timeout" in e["message"] for e in events)
    # Log standard: a problem is logged at WARNING or above, never echoed at INFO.
    reported = [r for r in caplog.records if "wifi timeout" in r.getMessage()]
    assert reported and all(r.levelno >= logging.WARNING for r in reported)


def test_log_stamps_are_utc():
    # Log standard: app.log stamps are UTC whatever the container's TZ.
    import logging
    import time
    assert logging.Formatter.converter is time.gmtime


def test_checkin_rejects_non_object(client):
    assert client.post("/api/device/checkin", json=[1, 2]).status_code == 400
    assert client.post("/api/device/checkin", content=b"nope",
                       headers={"Content-Type": "application/json"}).status_code == 400


def test_checkin_caps_field_sizes(client):
    client.post("/api/device/checkin", json={"error": "x" * 5000})
    st = client.get("/api/state").json()
    assert len(st["last_checkin"]["error"]) == 500


def test_head_supported_on_device_endpoints(client):
    r = client.head("/api/display.png")
    assert r.status_code == 200
    assert "x-image-version" in r.headers
    assert client.head("/api/display/meta").status_code == 200


def test_cycle_animal(client):
    st0 = client.get("/api/state").json()
    r = client.post("/api/cycle", json={"what": "animal"})
    assert r.status_code == 200
    assert r.json()["config"]["animal_offset"] == st0["config"]["animal_offset"] + 1
    assert client.post("/api/cycle", json={"what": "question"}).status_code == 400
    # The quote feature is gone: its cycle/veto targets are unknown now.
    assert client.post("/api/cycle", json={"what": "quote"}).status_code == 400
    assert client.post("/api/veto", json={"what": "quote"}).status_code == 400
    assert "quote" not in client.get("/api/state").json()["current"]
    assert client.post("/api/config", json={"show_quote": True}).status_code == 400


def test_veto_animal_image(client, monkeypatch):
    import datasources
    assert client.post("/api/veto", json={"what": "animal"}).status_code == 409  # nothing cached
    datasources._cache_write("animal.json", {"fetched_at": 0, "data": {"common_name": "fox"}})
    assert client.post("/api/veto", json={"what": "animal"}).status_code == 409  # no file known
    datasources._cache_write("animal.json", {"fetched_at": 0, "data": {
        "common_name": "fox", "file": "File:Vulpes a.tif"}})

    def render_picks_new():
        datasources._cache_write("animal.json", {"fetched_at": 0, "data": {
            "common_name": "fox", "file": "File:Vulpes b.tif"}})
        return {"image_version": 1}
    monkeypatch.setattr("renderer.render", lambda force=False: render_picks_new())
    r = client.post("/api/veto", json={"what": "animal"})
    assert r.status_code == 200
    assert state.load_config()["vetoed_images"] == ["File:Vulpes a.tif"]
    assert r.json()["current"]["animal"]["file"] == "File:Vulpes b.tif"
    assert client.post("/api/veto", json={"what": "weather"}).status_code == 400
    assert client.post("/api/unveto", json={"animal": "fox"}).status_code in (404, 405)


def test_rotate_animal_image(client, monkeypatch):
    import datasources
    assert client.post("/api/rotate", json={}).status_code == 409   # nothing cached
    datasources._cache_write("animal.json", {"fetched_at": 0, "data": {
        "common_name": "gorilla", "file": "File:Gorilla.tif"}})
    monkeypatch.setattr("renderer.render", lambda force=False: {"image_version": 1})
    assert client.post("/api/rotate", json={"degrees": 45}).status_code == 400
    r = client.post("/api/rotate", json={})
    assert r.status_code == 200
    assert r.json()["config"]["image_rotations"] == {"File:Gorilla.tif": 90}
    assert r.json()["current"]["animal"]["rotation"] == 90
    for _ in range(3):   # a full turn brings the entry back out of config
        r = client.post("/api/rotate", json={"degrees": 90})
    assert r.json()["config"]["image_rotations"] == {}
    assert client.get("/api/state").json()["current"]["animal"]["rotation"] == 0


def test_veto_animal_reports_no_replacement(client, monkeypatch):
    import datasources
    datasources._cache_write("animal.json", {"fetched_at": 0, "data": {
        "common_name": "fox", "file": "File:Vulpes a.tif"}})
    monkeypatch.setattr("renderer.render", lambda force=False: {"image_version": 1})
    assert client.post("/api/veto", json={"what": "animal"}).status_code == 502
    assert state.load_config()["vetoed_images"] == ["File:Vulpes a.tif"]  # still vetoed


def _showing(fake_data, file="File:Fox.tif"):
    """Put fake_data's animal on the panel (cache) as `file`, with a figure
    the art box can lean into."""
    import datasources
    from PIL import ImageDraw
    sheet = Image.new("RGB", (900, 1200), (238, 234, 224))
    ImageDraw.Draw(sheet).rectangle([350, 300, 550, 900], fill=(30, 30, 30))
    sheet.save(fake_data["animal"]["path"])
    fake_data["animal"]["file"] = file
    datasources._cache_write("animal.json", {"fetched_at": 0, "data": fake_data["animal"]})


def test_placement_bumps_the_version_only_when_its_image_is_on_the_panel(
        client, fake_data, monkeypatch):
    import datasources
    _showing(fake_data)
    ahead = {**fake_data["animal"], "file": "File:Hare.tif", "common_name": "hare"}
    monkeypatch.setattr(datasources, "find_animal", lambda f: {
        "File:Fox.tif": fake_data["animal"], "File:Hare.tif": ahead}.get(f))
    v0 = client.post("/api/render").json()["image_version"]

    r = client.post("/api/placement", json={"file": "File:Hare.tif", "layout": "hero",
                                            "x": 0.2, "scale": 1.4})
    assert r.status_code == 200
    assert r.json()["animal"]["placements"] == {"hero": {"x": 0.2, "y": 0.0, "scale": 1.4}}
    assert client.get("/api/display/meta").json()["image_version"] == v0   # not on the wall

    # The panel's own image, in a layout the panel isn't drawing: no change.
    r = client.post("/api/placement", json={"layout": "stack", "x": 0.2})
    assert r.json()["render"]["image_version"] == v0
    r = client.post("/api/placement", json={"x": 0.2})        # its current layout (hero)
    assert r.json()["render"]["image_version"] == v0 + 1
    assert r.json()["current"]["animal"]["placements"]["hero"]["x"] == 0.2
    r = client.post("/api/placement", json={"reset": True})
    assert r.json()["render"]["image_version"] == v0 + 2
    assert r.json()["animal"]["placements"] == {"stack": {"x": 0.2, "y": 0.0, "scale": 1.0}}
    # Saving the automatic fit by value stores nothing, and changes no pixels.
    r = client.post("/api/placement", json={"layout": "stack", "x": 0, "y": 0, "scale": 1})
    assert r.json()["render"]["image_version"] == v0 + 2
    assert state.load_config()["image_placements"] == {
        "File:Hare.tif": {"hero": {"x": 0.2, "y": 0.0, "scale": 1.4}}}


def test_placement_rejects_unknown_images_and_junk(client, fake_data):
    _showing(fake_data)
    assert client.post("/api/placement", json={"file": "File:Nope.tif", "x": 0.1}).status_code == 404
    assert client.post("/api/placement", json={"x": "left"}).status_code == 400
    assert client.post("/api/placement", json={"layout": "sideways", "x": 0.1}).status_code == 400


def _layers(client, **params):
    import base64
    r = client.get("/api/page-layers", params={"file": "File:Fox.tif", **params})
    assert r.status_code == 200, r.text
    j = r.json()

    def img(uri):
        return Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))).convert("RGBA")
    return j, img(j["page"]), img(j["art"]["image"])


def _composed(j, page, art):
    """What the browser shows while a finger is down: paper, the art clipped
    to the box, the page over it (the box's CSS fades left out)."""
    f = page.width / j["size"][0]
    canvas = Image.new("RGBA", page.size, j["paper"])
    x0, y0, x1, y1 = (round(v * f) for v in j["art"]["rect"])
    layer = Image.new("RGBA", page.size, (0, 0, 0, 0))
    layer.paste(art.resize((x1 - x0, y1 - y0)), (x0, y0))
    clip = Image.new("L", page.size, 0)
    clip.paste(255, tuple(round(v * f) for v in j["box"]))
    layer.putalpha(Image.composite(layer.getchannel("A"), clip, clip))
    return Image.alpha_composite(Image.alpha_composite(canvas, layer), page).convert("RGB")


def _mean_diff(a, b):
    from PIL import ImageChops, ImageStat
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3


def test_page_layers_compose_to_the_render_at_the_saved_placement(client, fake_data):
    """The layers the live drag moves are the page the panel draws: for a
    saved placement (pushed past the box, so the centre clamp applies), the
    box and placement match what the full render uses, and paper + art +
    page composes to that render (in the simulation's tones). The art moved by the placement shift
    a drag would apply no longer does, so the comparison can tell."""
    import json
    _showing(fake_data)
    # The daily view is placeable in landscape only (portrait shows it whole).
    for screen, layout, rotation in (("dashboard", "hero", 90), ("daily", "daily", 0)):
        client.post("/api/config", json={"rotation": rotation})
        client.post("/api/placement", json={"layout": layout, "x": 5, "y": -0.1,
                                            "scale": 1.3})
        r = client.get("/api/page-preview.png", params={"file": "File:Fox.tif", "screen": screen})
        j, page, art = _layers(client, screen=screen)
        w, h = j["size"]
        assert j["layout"] == layout
        assert [j["box"][0] / w, j["box"][1] / h, j["box"][2] / w, j["box"][3] / h] == \
            pytest.approx(json.loads(r.headers["X-Art-Box"]), abs=1e-3)
        assert j["placement"] == json.loads(r.headers["X-Placement"])
        assert 0 < j["placement"]["x"] < 1        # stored range caps at 1; the box edge sooner
        # The centre the browser clamps against: placement x of the box from auto.
        bw = j["box"][2] - j["box"][0]
        assert j["art"]["centre"][0] == pytest.approx(j["auto"][0] + j["placement"]["x"] * bw, abs=1)

        raw, _, _ = screens.page_preview(screen, screens.with_image_settings(
            {**fake_data["animal"]}, state.load_config()), state.load_config(), (w, h))
        raw = palette.simulate_tones(raw.resize(page.size, Image.LANCZOS))
        assert _mean_diff(_composed(j, page, art), raw) < 1.5, screen
        shifted = {**j, "art": {**j["art"], "rect": [
            v + (0.1 * bw if i % 2 == 0 else 0) for i, v in enumerate(j["art"]["rect"])]}}
        assert _mean_diff(_composed(shifted, page, art), raw) > 3, screen


def test_page_layers_refuse_prints_outside_the_queue(client, fake_data):
    _showing(fake_data)
    assert client.get("/api/page-layers", params={"file": "File:Nope.tif"}).status_code == 404
    assert client.get("/api/page-layers", params={"file": "File:Fox.tif",
                                                   "screen": "palette_test"}).status_code == 400


def test_daily_screen_places_its_own_layout_only_in_landscape(client, fake_data, monkeypatch):
    """With the daily screen up in landscape, dragging places prints in the
    daily layout. In portrait the print hangs whole as a polaroid: there is
    no layout, nothing to drag, and a placement for "the panel's layout" is
    refused rather than saved somewhere it doesn't show (DECISIONS §30)."""
    _showing(fake_data)
    client.post("/api/config", json={"screen": "daily", "rotation": 0})
    assert client.get("/api/state").json()["art_layout"] == {"key": "daily",
                                                             "name": "daily view"}
    j, _, _ = _layers(client)
    assert j["size"] == [1600, 1200]
    box, _ = screens.art_box(1600, 1200, "daily", True)
    assert j["box"] == list(box)

    client.post("/api/config", json={"rotation": 90})
    assert client.get("/api/state").json()["art_layout"] is None
    assert client.get("/api/page-layers", params={"file": "File:Fox.tif"}).status_code == 400
    assert client.post("/api/placement", json={"x": 0.1}).status_code == 400
    import datasources
    monkeypatch.setattr(datasources, "upcoming_animals", lambda *a, **k: [])
    assert client.get("/api/review").json()["layouts"]["daily"] is None


def test_review_queue_opens_at_the_first_unreviewed_and_moving_on_keeps_placement(
        client, fake_data, monkeypatch):
    """The queue opens at the first print not reviewed yet (a reviewed one
    and a day with no print are passed over; one still fetching is not).
    Moving on from a print marks it reviewed and leaves its placement as
    saved, so the queue then opens after it."""
    import datasources
    _showing(fake_data)
    hare = {**fake_data["animal"], "file": "File:Hare.tif", "common_name": "hare"}
    items = [{"slot": 1, "dates": ["2026-11-21"], "animal": fake_data["animal"], "pending": False},
             {"slot": 2, "dates": ["2026-11-22"], "animal": None, "pending": False},
             {"slot": 3, "dates": ["2026-11-23"], "animal": hare, "pending": False},
             {"slot": 4, "dates": ["2026-11-24"], "animal": None, "pending": True}]
    monkeypatch.setattr(datasources, "upcoming_animals", lambda *a, **k: items)
    monkeypatch.setattr(datasources, "find_animal",
                        lambda f: {"File:Hare.tif": hare, "File:Fox.tif": fake_data["animal"]}.get(f))
    monkeypatch.setattr(main, "_fetch_upcoming_in_background", lambda args: None)
    assert client.post("/api/mark-reviewed", json={"file": "File:Fox.tif"}).status_code == 200
    j = client.get("/api/review").json()
    assert [it["on_panel"] for it in j["items"]] == [True, False, False, False]
    assert j["start"] == 2
    client.post("/api/placement", json={"file": "File:Hare.tif", "layout": "hero", "x": 0.2})
    client.post("/api/mark-reviewed", json={"file": "File:Hare.tif"})
    client.post("/api/mark-reviewed", json={"file": "File:Hare.tif"})   # twice: still one mark
    j = client.get("/api/review").json()
    assert j["start"] == 3 and j["reviewed"] == 2
    assert j["items"][2]["animal"]["placements"] == {"hero": {"x": 0.2, "y": 0.0, "scale": 1.0}}
    assert state.load_config()["reviewed_images"] == ["File:Fox.tif", "File:Hare.tif"]


def test_page_preview_is_the_whole_page_and_refuses_prints_outside_the_queue(client, fake_data):
    """The review page's preview is the full page of the screen asked for,
    at the panel's orientation, with the art box the page maps a drag
    through; a file that is neither on the panel nor queued is refused."""
    import json
    _showing(fake_data)
    client.post("/api/config", json={"rotation": 90, "portrait_layout": "stack"})
    for screen, layout in (("dashboard", "stack"), ("daily", None)):
        r = client.get("/api/page-preview.png", params={"file": "File:Fox.tif", "screen": screen})
        assert r.status_code == 200
        w, h = Image.open(io.BytesIO(r.content)).size
        assert abs(w / h - 1200 / 1600) < 0.01
        if layout is None:   # the portrait daily view: shown whole, no box to drag in
            assert "X-Layout" not in r.headers and "X-Art-Box" not in r.headers
            continue
        assert r.headers["X-Layout"] == layout
        box, _ = screens.art_box(1200, 1600, layout, True)
        assert json.loads(r.headers["X-Art-Box"]) == pytest.approx(
            [box[0] / 1200, box[1] / 1600, box[2] / 1200, box[3] / 1600], abs=1e-3)
    assert client.get("/api/page-preview.png", params={"file": "File:Nope.tif"}).status_code == 404
    assert client.get("/api/page-preview.png", params={"file": "File:Fox.tif",
                                                       "screen": "palette_test"}).status_code == 400


def test_review_page_served_with_relative_urls(client):
    for path in ("/review", "/placement.js"):
        r = client.get(path)
        assert r.status_code == 200
        assert "'/api" not in r.text and '"/api' not in r.text
