"""Mutable state: config.json, event log, device check-in log.

Everything lives under config.DATA_DIR (a bind mount in Docker). Writes are
atomic (tmp + os.replace) per the bix-demucs convention. ndjson logs are
self-capping: on append, if the file exceeds the line cap it is rewritten
keeping the newest half — no cron needed.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path

import config
import screens

log = logging.getLogger("inkplate.state")

_lock = threading.Lock()

DEFAULT_CONFIG = {
    # How long the device deep-sleeps between refreshes.
    "refresh_minutes": config.DEFAULT_REFRESH_MINUTES,
    # Which screen renderer produces the frame (key into screens.SCREENS).
    "screen": screens.DEFAULT_SCREEN,
    # Tethered mode: device skips deep sleep and polls fast. For bring-up.
    "tethered": False,
    "tethered_poll_seconds": config.DEFAULT_TETHERED_POLL_SECONDS,
    # Weather location (editable in the pane).
    "latitude": config.DEFAULT_LATITUDE,
    "longitude": config.DEFAULT_LONGITUDE,
    "location_name": config.DEFAULT_LOCATION_NAME,
    # Now-playing widget (rage.bix). Off by default: at battery cadences the
    # song is stale anyway; enable on mains power with a short cadence.
    "show_now_playing": False,
    # Sequence offsets for the pane's "next animal"/"next question" cycle
    # buttons (layout testing) — advance the deterministic pick.
    "animal_offset": 0,
    "question_offset": 0,
    # How often the animal/question change, in days (1 = daily). Independent
    # of refresh_minutes, which governs how often the panel itself redraws —
    # the weather wants the hourly refresh, these do not.
    "animal_period_days": 1,
    "question_period_days": 1,
    # Monotonic content version. The device compares this against the version
    # it stored in RTC memory to decide whether to redraw (a redraw costs ~20s
    # and visible flashing, so unchanged content must never trigger one).
    "image_version": 1,
}


def _config_path() -> Path:
    return config.DATA_DIR / "config.json"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        on_disk = json.loads(_config_path().read_text())
        # Merge so new keys added in code get defaults without migration.
        cfg.update({k: v for k, v in on_disk.items() if k in DEFAULT_CONFIG})
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as e:
        log.error("config.json unreadable, using defaults: %s", e)
    if cfg["screen"] not in screens.SCREENS:
        log.warning("configured screen %r no longer exists, falling back", cfg["screen"])
        cfg["screen"] = screens.DEFAULT_SCREEN
    return cfg


def save_config(cfg: dict) -> None:
    with _lock:
        _atomic_write(_config_path(), json.dumps(cfg, indent=2))


def update_config(**changes) -> dict:
    """Validated read-modify-write. Returns the new config."""
    cfg = load_config()
    if "refresh_minutes" in changes:
        v = int(changes["refresh_minutes"])
        cfg["refresh_minutes"] = max(config.MIN_REFRESH_MINUTES, min(config.MAX_REFRESH_MINUTES, v))
    if "screen" in changes:
        if changes["screen"] not in screens.SCREENS:
            raise ValueError(f"unknown screen: {changes['screen']}")
        cfg["screen"] = changes["screen"]
    if "tethered" in changes:
        cfg["tethered"] = bool(changes["tethered"])
    if "tethered_poll_seconds" in changes:
        cfg["tethered_poll_seconds"] = max(5, min(600, int(changes["tethered_poll_seconds"])))
    if "latitude" in changes:
        v = float(changes["latitude"])
        if not -90 <= v <= 90:
            raise ValueError("latitude out of range")
        cfg["latitude"] = v
    if "longitude" in changes:
        v = float(changes["longitude"])
        if not -180 <= v <= 180:
            raise ValueError("longitude out of range")
        cfg["longitude"] = v
    if "location_name" in changes:
        cfg["location_name"] = str(changes["location_name"])[:60]
    if "show_now_playing" in changes:
        cfg["show_now_playing"] = bool(changes["show_now_playing"])
    for key in ("animal_offset", "question_offset"):
        if key in changes:
            cfg[key] = int(changes[key])
    for key in ("animal_period_days", "question_period_days"):
        if key in changes:
            cfg[key] = max(1, min(365, int(changes[key])))
    save_config(cfg)
    return cfg


def bump_version() -> dict:
    cfg = load_config()
    cfg["image_version"] += 1
    save_config(cfg)
    return cfg


# ── ndjson logs ───────────────────────────────────────────────────────────────

def _append_ndjson(path: Path, record: dict, cap: int) -> None:
    record = {"ts": time.time(), **record}
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")
        try:
            lines = path.read_text().splitlines()
            if len(lines) > cap:
                _atomic_write(path, "\n".join(lines[-cap // 2:]) + "\n")
        except OSError as e:
            log.warning("ndjson cap check failed for %s: %s", path, e)


def _read_ndjson(path: Path, limit: int) -> list[dict]:
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    out.reverse()  # newest first
    return out


def log_event(kind: str, message: str, **extra) -> None:
    """Server-side event trail (renders, config changes, device errors) —
    shown in the control pane so debugging doesn't require shell access."""
    log.info("event %s: %s", kind, message)
    _append_ndjson(config.DATA_DIR / "events.ndjson", {"kind": kind, "message": message, **extra},
                   config.MAX_EVENT_LINES)


def read_events(limit: int = 100) -> list[dict]:
    return _read_ndjson(config.DATA_DIR / "events.ndjson", limit)


def log_checkin(payload: dict) -> None:
    _append_ndjson(config.DATA_DIR / "checkins.ndjson", payload, config.MAX_CHECKIN_LINES)


def read_checkins(limit: int = 50) -> list[dict]:
    return _read_ndjson(config.DATA_DIR / "checkins.ndjson", limit)
