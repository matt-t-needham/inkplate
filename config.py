"""All tunables and env vars for inkplate-dash.

Follows the bix-ai convention: one module owns every knob, everything else
imports from here. Values are read at import time except paths used by tests,
which are read via functions so tests can monkeypatch DATA_DIR.
"""

import os
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
# DATA_DIR holds mutable state: config.json, rendered images, ndjson logs.
# In Docker this is a bind mount of inkplate_dash/data so it survives rebuilds.
DATA_DIR = Path(os.environ.get("INKPLATE_DATA_DIR", "data"))
LOG_DIR = Path(os.environ.get("INKPLATE_LOG_DIR", "logs"))

# ── Display hardware ──────────────────────────────────────────────────────────
# Inkplate 13SPECTRA panel: 13" E-Ink Spectra 6, landscape.
DISPLAY_WIDTH = int(os.environ.get("INKPLATE_WIDTH", "1600"))
DISPLAY_HEIGHT = int(os.environ.get("INKPLATE_HEIGHT", "1200"))

# ── Refresh cadence defaults (overridable live via the control pane) ──────────
DEFAULT_REFRESH_MINUTES = int(os.environ.get("INKPLATE_REFRESH_MINUTES", "60"))
# Bounds the control pane enforces. A Spectra full refresh takes ~20s and
# costs battery — sub-minute cadences make no sense on this hardware.
MIN_REFRESH_MINUTES = 1
MAX_REFRESH_MINUTES = 24 * 60

# Tethered mode: device skips deep sleep and polls fast (bring-up/debugging).
DEFAULT_TETHERED_POLL_SECONDS = int(os.environ.get("INKPLATE_TETHERED_POLL_SECONDS", "15"))

# ── Dashboard data sources ────────────────────────────────────────────────────
# Default location: seeded from the HTPC's IP geolocation at scaffold time
# (Portland, OR); editable live in the control pane.
# Defaults are central Portland; set INKPLATE_LAT/LON for your actual spot
# (the deployment passes them from bix-infra/.env, which is not committed).
DEFAULT_LATITUDE = float(os.environ.get("INKPLATE_LAT", "45.5152"))
DEFAULT_LONGITUDE = float(os.environ.get("INKPLATE_LON", "-122.6784"))
DEFAULT_LOCATION_NAME = os.environ.get("INKPLATE_LOCATION", "Portland, OR")

WEATHER_TTL_S = int(os.environ.get("INKPLATE_WEATHER_TTL_S", "1800"))
NOW_PLAYING_TTL_S = int(os.environ.get("INKPLATE_NOW_PLAYING_TTL_S", "90"))
# When the now-playing widget is enabled, renders go stale this fast instead
# of refresh_minutes (content-hash versioning means the device still only
# redraws when the pixels actually changed).
NOW_PLAYING_RENDER_TTL_S = int(os.environ.get("INKPLATE_NOW_PLAYING_RENDER_TTL_S", "120"))

# The rage.bix "Whats Playing?" service on the host (the :3007 tenant).
# The deployment sets the real LAN address via INKPLATE_RAGE_NOW_URL.
RAGE_NOW_URL = os.environ.get("INKPLATE_RAGE_NOW_URL",
                              "http://host.docker.internal:3007/api/now?channel=2")

# Contact for the User-Agent sent to public APIs (Wikimedia asks for one).
CONTACT = os.environ.get("INKPLATE_CONTACT", "https://github.com/matt-t-needham/inkplate")

# Width of the Commons thumbnail we request for the daily animal print.
ANIMAL_IMG_WIDTH = int(os.environ.get("INKPLATE_ANIMAL_IMG_WIDTH", "1000"))

# ── Retention ─────────────────────────────────────────────────────────────────
# ndjson logs are capped by line count on write (cheap, no cron needed).
MAX_CHECKIN_LINES = int(os.environ.get("INKPLATE_MAX_CHECKINS", "5000"))
MAX_EVENT_LINES = int(os.environ.get("INKPLATE_MAX_EVENTS", "5000"))

# ── Fonts ─────────────────────────────────────────────────────────────────────
# DejaVu ships in the Docker image (fonts-dejavu-core/-extra) and on the host.
# Michroma (the rage channel's display font) is vendored in ./fonts.
_APP_DIR = Path(__file__).resolve().parent

FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]
FONT_BOLD_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]
FONT_DISPLAY_PATHS = [
    str(_APP_DIR / "fonts" / "Michroma-Regular.ttf"),
]
# Weather Icons by Erik Flowers (SIL OFL 1.1) — erikflowers.github.io/weather-icons
FONT_WEATHER_PATHS = [
    str(_APP_DIR / "fonts" / "weathericons-regular-webfont.ttf"),
]
FONT_SERIF_ITALIC_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf",  # host dev fallback
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
