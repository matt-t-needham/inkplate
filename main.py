"""inkplate-dash — HTPC-side feeder for the Inkplate 13SPECTRA.

The device is a dumb poller: it wakes from deep sleep, GETs a pre-dithered
PNG, draws it, reports telemetry, and sleeps for however long the server told
it to. Every route the firmware touches lives under /api/ and is documented in
firmware/inkplate_dash/inkplate_dash.ino.

All URLs are served relative so the app works both directly (:3008/) and
behind the metrics nginx prefix (:9000/inkplate/).
"""

import asyncio
import io
import json
import logging
import logging.handlers
import os
import time
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

import config

# Logging before local imports, per house convention.
_fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
# Log stamps are UTC whatever TZ the container sets (TZ is for what the panel
# displays); the nightly log compiler windows app.log by these stamps.
logging.Formatter.converter = time.gmtime
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
import palette  # noqa: E402
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


@app.get("/review", response_class=HTMLResponse)
async def review_page():
    return (_STATIC / "review.html").read_text()


@app.get("/placement.js")
async def placement_js():
    return FileResponse(_STATIC / "placement.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})


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
        "portrait_layouts": list(screens.PORTRAIT_LAYOUTS),
        "render": renderer.meta(),
        "last_checkin": checkins[0] if checkins else None,
        "battery_percent": (state.battery_percent(checkins[0].get("battery_voltage"))
                            if checkins else None),
        "checkins": checkins,
        "events": state.read_events(limit=50),
        "current": _current(cfg),
    }


def _current(cfg: dict | None = None) -> dict:
    """What the dashboard is showing right now — for the pane's veto card.
    From the cache (no network on the 10 s poll)."""
    cfg = cfg or state.load_config()
    a = datasources.cached_animal()
    return {"animal": _print_summary(a, cfg) if a else None}


def _print_summary(a: dict, cfg: dict) -> dict:
    """What the pane and the review page show of one print, with its
    per-image settings."""
    f = a.get("file")
    return ({k: a.get(k) for k in ("common_name", "sci_name", "year", "artist",
                                   "collection", "file")}
            | {"rotation": (cfg.get("image_rotations") or {}).get(f, 0),
               "placement": ((cfg.get("image_placements") or {}).get(f)
                             or dict(screens.DEFAULT_PLACEMENT)),
               "reviewed": f in (cfg.get("reviewed_images") or [])})


async def _target_print(body: dict) -> dict:
    """The print a per-image action is for: body["file"] when given (it must
    be on the panel or looked ahead to by the review page), else the one on
    the panel now. 404/409 when there is no such print."""
    f = body.get("file")
    if f is not None:
        if not isinstance(f, str) or not f:
            raise HTTPException(400, "file must be a Commons file title")
        a = await asyncio.to_thread(datasources.find_print, f)
        if not a:
            raise HTTPException(404, "no such print on the panel or in the review list")
        return a
    a = datasources.cached_animal()
    if not a or not a.get("file"):
        raise HTTPException(409, "no animal image is currently showing")
    return a


def _on_panel(file: str) -> bool:
    a = datasources.cached_animal()
    return bool(a and a.get("file") == file)


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
    allowed = {"refresh_minutes", "refresh_anchor", "screen", "rotation", "portrait_layout",
               "tethered", "tethered_poll_seconds",
               "latitude", "longitude", "location_name",
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
    """Next animal: advance the daily animal sequence by one (persisted, so
    device and preview stay in sync). Re-renders immediately; the version
    bumps via the content hash."""
    body = await request.json()
    what = body.get("what")
    cfg = state.load_config()
    if what == "animal":
        cfg = await asyncio.to_thread(state.update_config,
                                      animal_offset=cfg["animal_offset"] + 1)
        state.log_event("config", f"cycled animal (offset {cfg['animal_offset']})")
        m = await _rerender("cycle")
    else:
        raise HTTPException(400, "what must be animal")
    return {"config": cfg, "render": m, "current": _current()}


@app.post("/api/veto")
async def api_veto(request: Request):
    """Ban what's on screen, permanently. Animal: the image's Commons file
    joins vetoed_images and is never picked again — the animal stays in
    rotation and is redrawn from another plate. The panel re-renders with a
    replacement immediately; 502 if none could be found."""
    body = await request.json()
    what = body.get("what")
    cfg = state.load_config()
    if what == "animal":
        a = await _target_print(body)
        await asyncio.to_thread(state.update_config,
                                vetoed_images=cfg["vetoed_images"] + [a["file"]])
        state.log_event("config", f"vetoed image: {a['file']} ({a.get('common_name')})")
        if not _on_panel(a["file"]):
            # An upcoming print: its slot is looked up again on the review
            # page's next load; the panel is unchanged.
            return {"render": renderer.meta(), "current": _current()}
        m = await _rerender("veto")
        new = datasources.cached_animal()
        if not new or new.get("file") == a["file"]:
            raise HTTPException(502, "image vetoed, but no replacement plate was "
                                     "found — the panel shows no animal")
    else:
        raise HTTPException(400, "what must be animal")
    return {"render": m, "current": _current()}


@app.post("/api/rotate")
async def api_rotate(request: Request):
    """Turn the image on screen by a quarter: some plates are scanned
    sideways (a landscape figure bound into a portrait book). The turn is
    stored per Commons file in image_rotations and accumulates — four clicks
    bring it back round. Re-renders immediately; the version bumps via the
    content hash."""
    body = await request.json()
    try:
        step = int(body.get("degrees", 90))
    except (TypeError, ValueError):
        raise HTTPException(400, "degrees must be an integer")
    if step % 90:
        raise HTTPException(400, "degrees must be a multiple of 90")
    a = await _target_print(body)
    cfg = state.load_config()
    rots = dict(cfg.get("image_rotations") or {})
    rots[a["file"]] = (int(rots.get(a["file"], 0)) + step) % 360
    cfg = await asyncio.to_thread(state.update_config, image_rotations=rots)
    state.log_event("config", f"rotated image {a['file']} to "
                              f"{cfg['image_rotations'].get(a['file'], 0)}°")
    m = await _rerender("rotate") if _on_panel(a["file"]) else renderer.meta()
    return {"config": cfg, "render": m, "current": _current(cfg),
            "print": _print_summary(a, cfg)}


@app.post("/api/placement")
async def api_placement(request: Request):
    """Save where a print sits in its art box: {x, y, scale} (see
    screens.normalize_placement), or {reset: true} for the automatic fit.
    Per Commons file, like rotation; `file` defaults to the print on the
    panel. Re-renders when that print is on the panel; the version bumps
    only if the pixels changed (content hash)."""
    body = await request.json()
    a = await _target_print(body)
    cfg = state.load_config()
    places = dict(cfg.get("image_placements") or {})
    if body.get("reset"):
        places.pop(a["file"], None)
    else:
        try:
            p = screens.normalize_placement(
                {k: body.get(k, d) for k, d in screens.DEFAULT_PLACEMENT.items()})
        except ValueError as e:
            raise HTTPException(400, str(e))
        if p:
            places[a["file"]] = p
        else:
            places.pop(a["file"], None)
    cfg = await asyncio.to_thread(state.update_config, image_placements=places)
    placed = cfg["image_placements"].get(a["file"])
    state.log_event("config", f"placed image {a['file']} at "
                              f"{placed or 'the automatic fit'}")
    m = await _rerender("placement") if _on_panel(a["file"]) else renderer.meta()
    return {"render": m, "current": _current(cfg), "print": _print_summary(a, cfg)}


@app.post("/api/reviewed")
async def api_reviewed(request: Request):
    """Mark a print reviewed (or not) on the review page: {file, reviewed}."""
    body = await request.json()
    if not isinstance(body.get("file"), str):
        raise HTTPException(400, "file is required")
    a = await _target_print(body)
    cfg = state.load_config()
    done = set(cfg.get("reviewed_images") or [])
    if body.get("reviewed", True):
        done.add(a["file"])
    else:
        done.discard(a["file"])
    cfg = await asyncio.to_thread(state.update_config, reviewed_images=sorted(done))
    return {"print": _print_summary(a, cfg)}


REVIEW_DAYS = 14


@app.get("/api/review")
async def api_review(days: int = REVIEW_DAYS):
    """The upcoming prints in the order they will hang, from today: the
    renderer's own slots and selection (datasources.upcoming_animals), each
    with its settings. Slow on first sight of a slot (it fetches from
    Commons); cached after."""
    cfg = state.load_config()
    try:
        items = await asyncio.to_thread(
            datasources.upcoming_animals, date.today(), min(max(days, 1), 60),
            cfg["animal_offset"], cfg["animal_period_days"], cfg["vetoed_images"])
    except Exception as e:
        log.error("review lookahead failed: %s", e, exc_info=True)
        raise HTTPException(502, f"could not look ahead: {e}")
    current = datasources.cached_animal()
    out = [{"slot": it["slot"], "dates": it["dates"],
            "on_panel": bool(it["animal"] and current
                             and it["animal"].get("file") == current.get("file")),
            "print": _print_summary(it["animal"], cfg) if it["animal"] else None}
           for it in items]
    prints = [o["print"] for o in out if o["print"]]
    return {"items": out, "total": len(prints),
            "reviewed": sum(1 for p in prints if p["reviewed"])}


PREVIEW_MAX = 900   # px, the long side of a print preview


@app.get("/api/print-preview.png")
async def api_print_preview(file: str, x: float | None = None, y: float | None = None,
                            scale: float | None = None, mode: str = "panel"):
    """One print alone in its art box, drawn by the dashboard's own code at
    the panel's size and layout — with the saved placement, or with x/y/scale
    when given (an unsaved draft from the Adjust control). mode "panel" is
    dithered to the six inks and shown as the panel will; "raw" is the RGB.
    The placement actually drawn (clamped to the box) is in X-Placement."""
    if mode not in ("raw", "panel"):
        raise HTTPException(400, "mode must be raw|panel")
    a = await asyncio.to_thread(datasources.find_print, file)
    if not a:
        raise HTTPException(404, "no such print on the panel or in the review list")
    cfg = state.load_config()
    animal = screens.with_image_settings(a, cfg)
    if x is not None or y is not None or scale is not None:
        saved = animal["placement"] or screens.DEFAULT_PLACEMENT
        draft = {"x": saved["x"] if x is None else x, "y": saved["y"] if y is None else y,
                 "scale": saved["scale"] if scale is None else scale}
        try:
            animal["placement"] = screens.normalize_placement(draft)
        except ValueError as e:
            raise HTTPException(400, str(e))

    def draw():
        img, placed = screens.art_preview(animal, cfg,
                                          renderer.upright_size(cfg["rotation"]))
        if mode == "panel":
            img = palette.simulate_panel(palette.dither_to_spectra(img))
        img.thumbnail((PREVIEW_MAX, PREVIEW_MAX))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue(), placed
    try:
        png, placed = await asyncio.to_thread(draw)
    except Exception as e:
        log.error("print preview failed for %s: %s", file, e, exc_info=True)
        raise HTTPException(500, f"preview failed: {e}")
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "no-store",
                             "X-Placement": json.dumps(placed)})



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
    poll = cfg["tethered_poll_seconds"] if cfg["tethered"] else renderer.sleep_seconds(cfg)
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
        log.warning("device reported an error: %s", payload["error"])
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
