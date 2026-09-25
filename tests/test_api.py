import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import main
import palette
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
    assert set(st) == {"config", "screens", "render", "last_checkin", "checkins", "events"}
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
                                         "location_name": "London", "show_now_playing": True})
    assert r.status_code == 200
    cfg = r.json()
    assert (cfg["latitude"], cfg["longitude"]) == (51.5, -0.12)
    assert cfg["location_name"] == "London"
    assert cfg["show_now_playing"] is True


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
    assert meta["sleep_seconds"] == state.load_config()["refresh_minutes"] * 60

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


def test_checkin_error_creates_event(client):
    client.post("/api/device/checkin", json={"boot_count": 1, "error": "wifi timeout"})
    events = client.get("/api/events").json()
    assert any(e["kind"] == "device_error" and "wifi timeout" in e["message"] for e in events)


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


def test_cycle_animal_and_question(client):
    st0 = client.get("/api/state").json()
    r = client.post("/api/cycle", json={"what": "animal"})
    assert r.status_code == 200
    assert r.json()["config"]["animal_offset"] == st0["config"]["animal_offset"] + 1

    r = client.post("/api/cycle", json={"what": "question"})
    assert r.status_code == 200
    assert r.json()["config"]["question_offset"] == 1

    assert client.post("/api/cycle", json={"what": "weather"}).status_code == 400


def test_cycle_question_changes_content_and_version(client):
    client.post("/api/config", json={"screen": "dashboard"})
    v1 = client.post("/api/render").json()["image_version"]
    r = client.post("/api/cycle", json={"what": "question"}).json()
    # different question -> different pixels -> hash-driven version bump
    assert r["render"]["image_version"] == v1 + 1
