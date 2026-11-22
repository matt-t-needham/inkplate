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


def test_animal_preview_reports_the_clamped_placement(client, fake_data):
    import json
    _showing(fake_data)
    r = client.get("/api/animal-preview.png", params={"file": "File:Fox.tif", "x": 5,
                                                      "mode": "raw"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    placed = json.loads(r.headers["X-Placement"])
    assert 0 < placed["x"] < 1        # stored range caps at 1; the box edge sooner
    # The preview is the art box of the layout asked for, else the panel's
    # own (portrait hero).
    def aspect(r):
        w, h = Image.open(io.BytesIO(r.content)).size
        return w / h
    box, _ = screens.art_box(1200, 1600, "hero", True)
    assert abs(aspect(r) - (box[2] - box[0]) / (box[3] - box[1])) < 0.01
    r = client.get("/api/animal-preview.png", params={"file": "File:Fox.tif",
                                                      "layout": "landscape", "mode": "raw"})
    box, _ = screens.art_box(1600, 1200, "landscape", True)
    assert abs(aspect(r) - (box[2] - box[0]) / (box[3] - box[1])) < 0.01
    assert client.get("/api/animal-preview.png", params={"file": "File:Nope.tif"}).status_code == 404


def test_daily_screen_adjusts_its_own_layout_in_the_panels_orientation(client, fake_data):
    """With the daily screen up, Adjust and the review page place prints in
    the daily layout, previewed at the panel's current orientation."""
    _showing(fake_data)
    for rotation, size in ((90, (1200, 1600)), (0, (1600, 1200))):
        client.post("/api/config", json={"screen": "daily", "rotation": rotation})
        assert client.get("/api/state").json()["art_layout"] == {"key": "daily",
                                                                 "name": "daily view"}
        r = client.get("/api/animal-preview.png", params={"file": "File:Fox.tif", "mode": "raw"})
        w, h = Image.open(io.BytesIO(r.content)).size
        box, _ = screens.art_box(*size, "daily", True)
        assert abs(w / h - (box[2] - box[0]) / (box[3] - box[1])) < 0.01


def test_review_marks_the_panel_image_and_round_trips_reviewed(client, fake_data, monkeypatch):
    import datasources
    _showing(fake_data)
    hare = {**fake_data["animal"], "file": "File:Hare.tif", "common_name": "hare"}

    def upcoming(*a, **k):
        return [{"slot": 1, "dates": ["2026-11-21"], "animal": fake_data["animal"],
                 "pending": False},
                {"slot": 2, "dates": ["2026-11-22"], "animal": hare, "pending": False}]
    monkeypatch.setattr(datasources, "upcoming_animals", upcoming)
    monkeypatch.setattr(datasources, "find_animal",
                        lambda f: {"File:Hare.tif": hare}.get(f))
    assert client.post("/api/mark-reviewed", json={"file": "File:Hare.tif"}).status_code == 200
    j = client.get("/api/review").json()
    assert [it["on_panel"] for it in j["items"]] == [True, False]
    assert j["items"][1]["animal"]["reviewed"] is True
    client.post("/api/mark-reviewed", json={"file": "File:Hare.tif", "reviewed": False})
    assert client.get("/api/review").json()["reviewed"] == 0


def test_review_page_served_with_relative_urls(client):
    for path in ("/review", "/placement.js"):
        r = client.get(path)
        assert r.status_code == 200
        assert "'/api" not in r.text and '"/api' not in r.text
