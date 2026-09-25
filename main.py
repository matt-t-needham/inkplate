"""inkplate-dash — HTPC-side feeder for the Inkplate 13SPECTRA.

The device is a dumb poller: it wakes from deep sleep, GETs a pre-dithered
PNG, draws it, reports telemetry, and sleeps for however long the server told
it to. Every route the firmware touches lives under /api/ and is documented in
firmware/inkplate_dash/inkplate_dash.ino.

All URLs are served relative so the app works both directly (:3008/) and
behind the metrics nginx prefix (:9000/inkplate/).
"""

import asyncio
import logging
import logging.handlers
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import config

# Logging before local imports, per house convention.
_fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format=_fmt)
log = logging.getLogger("inkplate")
try:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    _fh = logging.handlers.RotatingFileHandler(
        config.LOG_DIR / "app.log", maxBytes=10_000_000, backupCount=3,
    )
    _fh.setFormatter(logging.Formatter(_fmt))
    logging.getLogger().addHandler(_fh)
except OSError as e:
    log.warning("file logging unavailable: %s", e)

import renderer  # noqa: E402
import screens  # noqa: E402
import state  # noqa: E402

app = FastAPI(title="inkplate-dash")

_STATIC = Path(__file__).parent / "static"

# Render is CPU-bound (~150ms); one at a time is plenty and avoids a stampede
# if the pane and the device ask simultaneously.
_render_lock = asyncio.Lock()


async def _ensure_fresh() -> dict:
    async with _render_lock:
        return await asyncio.to_thread(renderer.ensure_fresh)


# ── UI ────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return (_STATIC / "index.html").read_text()


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ── Control-pane API ──────────────────────────────────────────────────────────

@app.get("/api/state")
async def api_state():
    cfg = state.load_config()
    checkins = state.read_checkins(limit=20)
    return {
        "config": cfg,
        "screens": list(screens.SCREENS.keys()),
        "render": renderer.meta(),
        "last_checkin": checkins[0] if checkins else None,
        "checkins": checkins,
        "events": state.read_events(limit=50),
    }


@app.post("/api/config")
async def api_config(request: Request):
    body = await request.json()
    allowed = {"refresh_minutes", "screen", "tethered", "tethered_poll_seconds",
               "latitude", "longitude", "location_name", "show_now_playing",
               "animal_period_days", "question_period_days"}
    changes = {k: v for k, v in body.items() if k in allowed}
    if not changes:
        raise HTTPException(400, "no recognized config keys")
    try:
        cfg = await asyncio.to_thread(state.update_config, **changes)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    state.log_event("config", f"config updated: {changes}")
    return cfg


@app.post("/api/render")
async def api_render():
    """Manual push: re-render now and bump the version so the device redraws
    at its next wake (or within seconds, in tethered mode)."""
    async with _render_lock:
        try:
            m = await asyncio.to_thread(renderer.render, True)
        except Exception as e:
            log.error("manual render failed: %s", e, exc_info=True)
            raise HTTPException(500, f"render failed: {e}")
    return m


@app.post("/api/cycle")
async def api_cycle(request: Request):
    """Advance the daily animal or question sequence by one — the pane's
    layout-testing buttons. Persists (device and preview stay in sync);
    re-renders immediately, version bumps via the content hash."""
    body = await request.json()
    what = body.get("what")
    if what not in ("animal", "question"):
        raise HTTPException(400, "what must be animal|question")
    key = f"{what}_offset"
    cfg = state.load_config()
    cfg = await asyncio.to_thread(state.update_config, **{key: cfg[key] + 1})
    state.log_event("config", f"cycled {what} (offset {cfg[key]})")
    async with _render_lock:
        try:
            m = await asyncio.to_thread(renderer.render)
        except Exception as e:
            log.error("cycle render failed: %s", e, exc_info=True)
            raise HTTPException(500, f"render failed: {e}")
    return {"config": cfg, "render": m}


@app.get("/api/preview.png")
async def api_preview(mode: str = "panel"):
    if mode not in ("raw", "dithered", "panel"):
        raise HTTPException(400, "mode must be raw|dithered|panel")
    await _ensure_fresh()
    path = renderer.preview_path(mode)
    if not path.exists():
        raise HTTPException(404, "no render available")
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "no-store"})


# ── Device API (what the firmware calls) ──────────────────────────────────────

def _device_meta(cfg: dict, m: dict | None) -> dict:
    poll = cfg["tethered_poll_seconds"] if cfg["tethered"] else cfg["refresh_minutes"] * 60
    return {
        "image_version": m["image_version"] if m else cfg["image_version"],
        "sleep_seconds": poll,
        "tethered": cfg["tethered"],
    }


# HEAD included so `curl -I` probes work (FastAPI doesn't add it to GETs).
@app.api_route("/api/display/meta", methods=["GET", "HEAD"])
async def api_display_meta():
    """Cheap version check (~100 bytes). The firmware calls this first and
    skips the ~200KB image download + 20s panel refresh when nothing changed."""
    cfg = state.load_config()
    try:
        m = await _ensure_fresh()
    except Exception:
        m = renderer.meta()  # fall back to whatever we have; error already logged
    return _device_meta(cfg, m)


@app.api_route("/api/display.png", methods=["GET", "HEAD"])
async def api_display():
    """The frame itself: indexed PNG, exactly the six Spectra colors."""
    cfg = state.load_config()
    try:
        m = await _ensure_fresh()
    except Exception as e:
        raise HTTPException(500, f"no frame available: {e}")
    dm = _device_meta(cfg, m)
    return FileResponse(
        renderer.dithered_path(), media_type="image/png",
        headers={
            "Cache-Control": "no-store",
            "X-Image-Version": str(dm["image_version"]),
            "X-Sleep-Seconds": str(dm["sleep_seconds"]),
        },
    )


@app.post("/api/device/checkin")
async def api_checkin(request: Request):
    """Telemetry from the device, sent once per wake. Free-form JSON — the
    firmware sends battery_voltage, rssi, boot_count, wake_reason, shown_version,
    duration_ms, fw_version, and error (null unless something went wrong)."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "body must be JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "body must be a JSON object")
    # Cap stored size; a runaway firmware bug shouldn't fill the disk.
    payload = {str(k)[:64]: (v if isinstance(v, (int, float, bool, type(None))) else str(v)[:500])
               for k, v in list(payload.items())[:32]}
    await asyncio.to_thread(state.log_checkin, payload)
    if payload.get("error"):
        state.log_event("device_error", f"device reported: {payload['error']}", **payload)
    cfg = state.load_config()
    return _device_meta(cfg, renderer.meta())


@app.get("/api/checkins")
async def api_checkins(limit: int = 50):
    return state.read_checkins(limit=min(max(limit, 1), 500))


@app.get("/api/events")
async def api_events(limit: int = 100):
    return state.read_events(limit=min(max(limit, 1), 500))


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.error("unhandled error on %s: %s", request.url.path, exc, exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "internal error"})
