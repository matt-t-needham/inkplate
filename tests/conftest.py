import os
import sys
import tempfile
from pathlib import Path

# Point data/log dirs at a throwaway location BEFORE config is imported, so
# importing main.py never writes into the repo tree.
_session_tmp = tempfile.mkdtemp(prefix="inkplate-test-")
os.environ.setdefault("INKPLATE_DATA_DIR", str(Path(_session_tmp) / "data"))
os.environ.setdefault("INKPLATE_LOG_DIR", str(Path(_session_tmp) / "logs"))

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import config  # noqa: E402


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """Fresh isolated DATA_DIR per test."""
    d = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", d)
    return d


FAKE_WEATHER = {
    "current_c": 21.4,
    "current_code": 2,
    "days": [
        {"date": "2026-09-17", "code": 0, "tmax": 28.1, "tmin": 11.5,
         "sunrise": "2026-09-17T06:53", "sunset": "2026-09-17T19:22"},
        {"date": "2026-09-18", "code": 61, "tmax": 24.1, "tmin": 11.8,
         "sunrise": "2026-09-18T06:54", "sunset": "2026-09-18T19:20"},
        {"date": "2026-09-19", "code": 95, "tmax": 26.4, "tmin": 10.6,
         "sunrise": "2026-09-19T06:55", "sunset": "2026-09-19T19:18"},
    ],
}

FAKE_NOW = {"artist": "Magic Dirt", "song": "Watch Out Boys", "year": "2003",
            "channel_name": "RAGE"}


@pytest.fixture()
def fake_data(tmp_path, monkeypatch):
    """Stub every external fetcher so screens/API tests stay offline.
    Tests of the real fetchers monkeypatch datasources._get_json/_get_bytes
    instead and must not use this fixture."""
    from PIL import Image

    import datasources

    art = tmp_path / "fake_animal.jpg"
    Image.new("RGB", (300, 220), (240, 235, 220)).save(art)
    fake_animal = {"path": str(art), "common_name": "fox",
                   "sci_name": "Vulpes vulpes", "year": "1864", "artist": None,
                   "place": "Amsterdam", "collection": "iz",
                   "native": "the Northern Hemisphere", "for_date": "2026-09-17"}

    monkeypatch.setattr(datasources, "get_weather", lambda lat, lon: FAKE_WEATHER)
    monkeypatch.setattr(datasources, "get_animal", lambda day=None, offset=0, period_days=1: fake_animal)
    monkeypatch.setattr(datasources, "get_now_playing", lambda: FAKE_NOW)
    return {"weather": FAKE_WEATHER, "animal": fake_animal, "now": FAKE_NOW}
