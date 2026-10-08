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

import datasources  # noqa: E402
import quotes  # noqa: E402
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
        "battery_percent": (state.battery_percent(checkins[0].get("battery_voltage"))
                            if checkins else None),
        "checkins": checkins,
        "events": state.read_events(limit=50),
        "current": _current(cfg),
    }


def _current(cfg: dict) -> dict:
    """What the dashboard is showing right now — for the pane's veto card.
    Both come from caches (no network on the 10 s poll)."""
    a = datasources.cached_animal()
    q = quotes.cached_quote()
    return {
        "animal": {k: a.get(k) for k in ("common_name", "sci_name", "year", "artist",
                                          "collection")} if a else None,
        "quote": {k: q.get(k) for k in ("text", "author", "work", "year",
                                         "qotd_date")} if q else None,
        "vetoed_animals": cfg["vetoed_animals"],
        "vetoed_quotes": cfg["vetoed_quotes"],
    }


async def _rerender(why: str) -> dict:
    async with _render_lock:
        try:
            return await asyncio.to_thread(renderer.render)
        except Exception as e:
            log.error("%s render failed: %s", why, e, exc_info=True)
            raise HTTPException(500, f"render failed: {e}")


@app.post("/api/config")
async def api_config(request: Request):
    body = await request.json()
    allowed = {"refresh_minutes", "screen", "tethered", "tethered_poll_seconds",
               "latitude", "longitude", "location_name", "show_now_playing",
               "animal_period_days",
               "show_snow", "snow_latitude", "snow_longitude", "snow_location_name"}
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
    """Advance the daily animal sequence by one — the pane's layout-testing
    button. Persists (device and preview stay in sync);
    re-renders immediately, version bumps via the content hash."""
    body = await request.json()
    what = body.get("what")
    if what != "animal":
        raise HTTPException(400, "what must be animal")
    key = f"{what}_offset"
    cfg = state.load_config()
    cfg = await asyncio.to_thread(state.update_config, **{key: cfg[key] + 1})
    state.log_event("config", f"cycled {what} (offset {cfg[key]})")
    m = await _rerender("cycle")
    return {"config": cfg, "render": m}


@app.post("/api/veto")
async def api_veto(request: Request):
    """Ban what's on screen. Animal: its common name joins vetoed_animals and
    the rotation skips it from now on. Quote: its text joins vetoed_quotes and
    is never picked again. Undo either with /api/unveto. The panel re-renders
    with a replacement immediately."""
    body = await request.json()
    what = body.get("what")
    cfg = state.load_config()
    if what == "animal":
        a = datasources.cached_animal()
        if not a or not a.get("common_name"):
            raise HTTPException(409, "no animal is currently showing")
        name = a["common_name"]
        cfg = await asyncio.to_thread(state.update_config,
                                      vetoed_animals=cfg["vetoed_animals"] + [name])
        state.log_event("config", f"vetoed animal: {name}")
    elif what == "quote":
        q = quotes.cached_quote()
        if not q or not q.get("text"):
            raise HTTPException(409, "no quote is currently showing")
        await asyncio.to_thread(state.update_config,
                                vetoed_quotes=cfg["vetoed_quotes"] + [q["text"]])
        state.log_event("config", f"vetoed quote: {q['text']} — {q.get('author')}")
    else:
        raise HTTPException(400, "what must be animal|quote")
    m = await _rerender("veto")
    return {"render": m, "current": _current(state.load_config())}


@app.post("/api/unveto")
async def api_unveto(request: Request):
    """Restore a vetoed animal ({"animal": name}) or quote ({"quote": text})."""
    body = await request.json()
    cfg = state.load_config()
    if "animal" in body:
        name = body["animal"]
        if name not in cfg["vetoed_animals"]:
            raise HTTPException(404, "not vetoed")
        cfg = await asyncio.to_thread(
            state.update_config,
            vetoed_animals=[n for n in cfg["vetoed_animals"] if n != name])
        state.log_event("config", f"restored animal: {name}")
    elif "quote" in body:
        text = body["quote"]
        if text not in cfg["vetoed_quotes"]:
            raise HTTPException(404, "not vetoed")
        cfg = await asyncio.to_thread(
            state.update_config,
            vetoed_quotes=[t for t in cfg["vetoed_quotes"] if t != text])
        state.log_event("config", f"restored quote: {text}")
    else:
        raise HTTPException(400, "body must name an animal or a quote")
    return {"config": cfg}


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
