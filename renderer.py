"""Render orchestration: screen → RGB frame → dithered frame → cached PNGs.

Renders are cached on disk under DATA_DIR/render/ and only redone when forced
(control pane "Render now") or when the cache predates the current wake slot
(see slot_window). The device endpoint always serves the cached dithered PNG, so a
device wake never waits on a render.

Files written per render (atomically, tmp + replace):
  render/raw.png       — full-color RGB frame (control-pane "raw" preview)
  render/dithered.png  — indexed 6-color PNG (control-pane "dithered" preview)
  render/panel.png     — muted-palette simulation (control-pane preview)
  render/frame.png     — dithered.png turned to the panel's native 1600x1200
                         orientation: what the device downloads
  render/meta.json     — {rendered_at, screen, rotation, image_version, render_ms}

Rotation: the screen is drawn upright at whatever size the hanging calls for
(1200x1600 when the panel is on its side) and the three previews stay upright
— they show what is on the wall. Only frame.png is turned, by the configured
rotation, so the firmware always receives a native-orientation frame and the
device protocol is untouched. config.rotation is the panel's clockwise turn
from landscape; PIL's rotate() is anticlockwise, so rotating the upright
image by that same angle is what puts it right when the panel is turned.
"""

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import config
import palette
import screens
import state

log = logging.getLogger("inkplate.renderer")


def _render_dir() -> Path:
    return config.DATA_DIR / "render"


def meta() -> dict | None:
    try:
        return json.loads((_render_dir() / "meta.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_png(img, path: Path) -> None:
    tmp = path.with_suffix(".tmp.png")
    img.save(tmp, format="PNG", optimize=True)
    os.replace(tmp, path)


def render(force_new_version: bool = False) -> dict:
    """Render the configured screen. Returns the new meta dict.

    Versioning is content-hash based: image_version bumps only when the
    dithered pixels actually differ from the previous render, so the device —
    which redraws only on a version change — never burns a ~20s panel refresh
    on identical content. force_new_version bumps regardless (the pane's
    "Render now → push": a guaranteed redraw, e.g. after a garbled screen).
    """
    cfg = state.load_config()
    screen_name = cfg["screen"]
    rotation = cfg["rotation"]
    t0 = time.monotonic()

    img = screens.SCREENS[screen_name](*upright_size(rotation))
    dithered = palette.dither_to_spectra(img)
    panel = palette.simulate_panel(dithered)
    frame = to_panel_frame(dithered, rotation)

    out = _render_dir()
    out.mkdir(parents=True, exist_ok=True)
    _save_png(img, out / "raw.png")
    _save_png(dithered, out / "dithered.png")
    _save_png(panel, out / "panel.png")
    _save_png(frame, out / "frame.png")

    # Hash the raw pixel buffer, not the PNG bytes — encoder settings can't
    # cause false "changed" verdicts that way. The device frame, so a
    # rotation change (same pixels, turned) is a version bump too.
    content_hash = hashlib.sha256(frame.tobytes()).hexdigest()
    prev = meta()
    changed = prev is None or prev.get("content_hash") != content_hash
    if force_new_version or changed:
        cfg = state.bump_version()

    render_ms = int((time.monotonic() - t0) * 1000)
    m = {
        "rendered_at": time.time(),
        "screen": screen_name,
        "rotation": rotation,
        "image_version": cfg["image_version"],
        "content_hash": content_hash,
        "render_ms": render_ms,
        "width": config.DISPLAY_WIDTH,
        "height": config.DISPLAY_HEIGHT,
    }
    tmp = out / "meta.json.tmp"
    tmp.write_text(json.dumps(m, indent=2))
    os.replace(tmp, out / "meta.json")

    state.log_event("render", f"rendered screen={screen_name} in {render_ms}ms "
                              f"version={cfg['image_version']}"
                              f"{' (content unchanged)' if not (changed or force_new_version) else ''}")
    return m


def slot_window(now: datetime, minutes: int, anchor: str) -> tuple[datetime, datetime]:
    """(start of the current wake slot, start of the next) for a naive local
    `now`. Slots run every `minutes` from `anchor` ("HH:MM") and restart at
    the anchor each day, so with a 150-minute cadence the last slot before
    06:00 is 04:30 and the gap to 06:00 is short. Wall-clock arithmetic: on
    a DST change day the slots after the change shift by the hour."""
    hh, mm = (int(p) for p in anchor.split(":"))
    day = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if day > now:
        day -= timedelta(days=1)
    step = timedelta(minutes=minutes)
    start = day + ((now - day) // step) * step
    return start, min(start + step, day + timedelta(days=1))


def schedule(cfg: dict, now: float | None = None) -> tuple[float, float]:
    """(current slot start, next slot start) as epoch seconds. A wake within
    WAKE_GRACE_S before a slot counts as that slot (the device clock drifts)."""
    now = time.time() if now is None else now
    start, nxt = slot_window(datetime.fromtimestamp(now + config.WAKE_GRACE_S),
                             cfg["refresh_minutes"], cfg["refresh_anchor"])
    return start.timestamp(), nxt.timestamp()


def sleep_seconds(cfg: dict, now: float | None = None) -> int:
    """How long the device should sleep: to the next wake slot."""
    now = time.time() if now is None else now
    return max(1, round(schedule(cfg, now)[1] - now))


def ensure_fresh() -> dict:
    """Render if there is no cached frame, the screen changed, or the cache
    predates the current wake slot. Returns current meta."""
    cfg = state.load_config()
    m = meta()
    slot_start, _ = schedule(cfg)
    stale = (
        m is None
        or m.get("screen") != cfg["screen"]
        or m.get("rotation", 0) != cfg["rotation"]
        or m.get("rendered_at", 0) < slot_start - config.WAKE_GRACE_S
        or not dithered_path().exists()
    )
    if stale:
        try:
            return render()
        except Exception as e:
            log.error("render failed: %s", e, exc_info=True)
            state.log_event("error", f"render failed: {e}")
            if m is not None and dithered_path().exists():
                return m  # serve the stale frame rather than nothing
            raise
    return m


def upright_size(rotation: int) -> tuple[int, int]:
    """Frame size the screen is drawn at: the panel's own size, with width
    and height swapped when it hangs on its side."""
    if rotation in (90, 270):
        return config.DISPLAY_HEIGHT, config.DISPLAY_WIDTH
    return config.DISPLAY_WIDTH, config.DISPLAY_HEIGHT


def to_panel_frame(img, rotation: int):
    """Turn an upright frame into the panel's native orientation. Lossless
    for the right angles (Pillow transposes); a 'P' image keeps its palette."""
    if rotation % 360 == 0:
        return img
    return img.rotate(rotation, expand=True)


def dithered_path() -> Path:
    """The device frame: dithered, in the panel's native orientation."""
    return _render_dir() / "frame.png"


def preview_path(mode: str) -> Path:
    name = {"raw": "raw.png", "dithered": "dithered.png", "panel": "panel.png"}[mode]
    return _render_dir() / name
