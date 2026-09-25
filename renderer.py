"""Render orchestration: screen → RGB frame → dithered frame → cached PNGs.

Renders are cached on disk under DATA_DIR/render/ and only redone when forced
(control pane "Render now") or when the cache is older than the refresh
cadence. The device endpoint always serves the cached dithered PNG, so a
device wake never waits on a render.

Files written per render (atomically, tmp + replace):
  render/raw.png       — full-color RGB frame (control-pane "raw" preview)
  render/dithered.png  — indexed 6-color PNG (what the device downloads)
  render/panel.png     — muted-palette simulation (control-pane preview)
  render/meta.json     — {rendered_at, screen, image_version, render_ms}
"""

import hashlib
import json
import logging
import os
import time
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
    t0 = time.monotonic()

    img = screens.SCREENS[screen_name](config.DISPLAY_WIDTH, config.DISPLAY_HEIGHT)
    dithered = palette.dither_to_spectra(img)
    panel = palette.simulate_panel(dithered)

    out = _render_dir()
    out.mkdir(parents=True, exist_ok=True)
    _save_png(img, out / "raw.png")
    _save_png(dithered, out / "dithered.png")
    _save_png(panel, out / "panel.png")

    # Hash the raw pixel buffer, not the PNG bytes — encoder settings can't
    # cause false "changed" verdicts that way.
    content_hash = hashlib.sha256(dithered.tobytes()).hexdigest()
    prev = meta()
    changed = prev is None or prev.get("content_hash") != content_hash
    if force_new_version or changed:
        cfg = state.bump_version()

    render_ms = int((time.monotonic() - t0) * 1000)
    m = {
        "rendered_at": time.time(),
        "screen": screen_name,
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


def ensure_fresh() -> dict:
    """Render if there is no cached frame, the screen changed, or the cache is
    older than the refresh cadence. Returns current meta."""
    cfg = state.load_config()
    m = meta()
    # With now-playing enabled the render (not the device) refreshes fast —
    # content-hash versioning means the device still only redraws on change.
    ttl_s = (config.NOW_PLAYING_RENDER_TTL_S if cfg.get("show_now_playing")
             else cfg["refresh_minutes"] * 60)
    stale = (
        m is None
        or m.get("screen") != cfg["screen"]
        or (time.time() - m.get("rendered_at", 0)) > ttl_s
        or not (_render_dir() / "dithered.png").exists()
    )
    if stale:
        try:
            return render()
        except Exception as e:
            log.error("render failed: %s", e, exc_info=True)
            state.log_event("error", f"render failed: {e}")
            if m is not None and (_render_dir() / "dithered.png").exists():
                return m  # serve the stale frame rather than nothing
            raise
    return m


def dithered_path() -> Path:
    return _render_dir() / "dithered.png"


def preview_path(mode: str) -> Path:
    name = {"raw": "raw.png", "dithered": "dithered.png", "panel": "panel.png"}[mode]
    return _render_dir() / name
