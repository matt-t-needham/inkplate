import json

import pytest

import state


def test_defaults_when_no_file(data_dir):
    cfg = state.load_config()
    assert cfg == state.DEFAULT_CONFIG


def test_update_and_reload(data_dir):
    state.update_config(refresh_minutes=30, screen="palette_test", tethered=True)
    cfg = state.load_config()
    assert cfg["refresh_minutes"] == 30
    assert cfg["screen"] == "palette_test"
    assert cfg["tethered"] is True


def test_refresh_minutes_clamped(data_dir):
    assert state.update_config(refresh_minutes=0)["refresh_minutes"] == 1
    assert state.update_config(refresh_minutes=99999)["refresh_minutes"] == 24 * 60


def test_unknown_screen_rejected(data_dir):
    with pytest.raises(ValueError):
        state.update_config(screen="nope")


def test_corrupt_config_falls_back(data_dir):
    data_dir.mkdir(parents=True)
    (data_dir / "config.json").write_text("{not json")
    assert state.load_config() == state.DEFAULT_CONFIG


def test_version_bump(data_dir):
    v0 = state.load_config()["image_version"]
    assert state.bump_version()["image_version"] == v0 + 1


def test_checkin_roundtrip_newest_first(data_dir):
    state.log_checkin({"boot_count": 1})
    state.log_checkin({"boot_count": 2})
    got = state.read_checkins()
    assert [c["boot_count"] for c in got] == [2, 1]
    assert all("ts" in c for c in got)


def test_ndjson_cap(data_dir, monkeypatch):
    import config
    monkeypatch.setattr(config, "MAX_EVENT_LINES", 10)
    for i in range(25):
        state.log_event("render", f"e{i}")
    lines = (data_dir / "events.ndjson").read_text().splitlines()
    assert len(lines) <= 11  # cap/2 kept after trim, +1 for the triggering append
    assert json.loads(lines[-1])["message"] == "e24"  # newest survive
