"""inkplate-dash — HTPC-side feeder for the Inkplate 13SPECTRA.

The device is a dumb poller: it wakes from deep sleep, GETs a pre-dithered
PNG, draws it, reports telemetry, and sleeps for however long the server told
it to. Every route the firmware touches lives under /api/ and is documented in
firmware/inkplate_dash/inkplate_dash.ino.

All URLs are served relative so the app works both directly (:3008/) and
behind the metrics nginx prefix (:9000/inkplate/).
"""

import asyncio
import base64
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
from PIL import Image

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
    checkins = state.read_checkins(limit=5)   # newest first; skips a torn line
    return {
        "config": cfg,
        "screens": list(screens.SCREENS.keys()),
        "portrait_layouts": list(screens.PORTRAIT_LAYOUTS),
        "art_layout": _art_layout(cfg),
        "render": renderer.meta(),
        "last_checkin": checkins[0] if checkins else None,
        "battery_percent": (state.battery_percent(checkins[0].get("battery_voltage"))
                            if checkins else None),
        "current": _current(cfg),
    }


def _layout_summary(key: str | None) -> dict | None:
    """{key, name} for an art layout; None when the page has none (the
    portrait daily view shows its print whole, so it can't be dragged)."""
    return {"key": key, "name": screens.ART_LAYOUT_NAMES.get(key, key)} if key else None


def _art_layout(cfg: dict) -> dict | None:
    """The art layout the panel draws now: the one dragging the print on the
    pane places it in, or None when the panel's print can't be placed."""
    return _layout_summary(screens.panel_layout(cfg, renderer.upright_size(cfg["rotation"])))


def _layout_param(v, cfg: dict) -> str:
    """A request's art layout: one of screens.ART_LAYOUTS, else 400; None
    means the panel's current one (400 when it has none)."""
    if v is None:
        cur = _art_layout(cfg)
        if cur is None:
            raise HTTPException(400, "the panel's page shows the print whole; "
                                     "it has no placement")
        return cur["key"]
    if v not in screens.ART_LAYOUTS:
        raise HTTPException(400, f"layout must be one of {', '.join(screens.ART_LAYOUTS)}")
    return v


def _current(cfg: dict | None = None) -> dict:
    """What the dashboard is showing right now — for the pane's veto card.
    From the cache (no network on the 10 s poll)."""
    cfg = cfg or state.load_config()
    a = datasources.cached_animal()
    return {"animal": _animal_summary(a, cfg) if a else None}


def _animal_summary(a: dict, cfg: dict) -> dict:
    """What the pane and the review page show of one animal, with its
    per-image settings ("placements" {layout: placement}; a layout with no
    entry is the automatic fit)."""
    a = screens.with_image_settings(a, cfg)
    return ({k: a.get(k) for k in ("common_name", "sci_name", "year", "artist",
                                   "collection", "file", "rotation", "placements")}
            | {"reviewed": a.get("file") in cfg["reviewed_images"]})


async def _target_animal(body: dict) -> dict:
    """The animal a per-image action is for: body["file"] when given (it must
    be on the panel or upcoming on the review page), else the one on
    the panel now. 404/409 when there is no such animal."""
    f = body.get("file")
    if f is not None:
        if not isinstance(f, str) or not f:
            raise HTTPException(400, "file must be a Commons file title")
        a = await asyncio.to_thread(datasources.find_animal, f)
        if not a:
            raise HTTPException(404, "no such image on the panel or in the review list")
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
    if what == "animal":
        a = await _target_animal(body)
        await asyncio.to_thread(state.mutate, lambda cfg: {
            "vetoed_images": cfg["vetoed_images"] + [a["file"]]})
        state.log_event("config", f"vetoed image: {a['file']} ({a.get('common_name')})")
        if not _on_panel(a["file"]):
            # An upcoming image: its slot is fetched again from the review
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
    a = await _target_animal(body)
    cfg = await asyncio.to_thread(state.mutate, lambda cfg: {"image_rotations": {
        **cfg["image_rotations"],
        a["file"]: (int(cfg["image_rotations"].get(a["file"], 0)) + step) % 360}})
    state.log_event("config", f"rotated image {a['file']} to "
                              f"{cfg['image_rotations'].get(a['file'], 0)}°")
    m = await _rerender("rotate") if _on_panel(a["file"]) else renderer.meta()
    return {"config": cfg, "render": m, "current": _current(cfg),
            "animal": _animal_summary(a, cfg)}


@app.post("/api/placement")
async def api_placement(request: Request):
    """Save where an image sits in one layout's art box: {layout, x, y,
    scale} (see screens.normalize_placement), or {layout, reset: true} for
    the automatic fit. Per Commons file and art layout; `file` defaults to
    the image on the panel and `layout` to the panel's current one.
    Re-renders when that image is on the panel."""
    body = await request.json()
    layout = _layout_param(body.get("layout"), state.load_config())
    a = await _target_animal(body)
    try:
        p = None if body.get("reset") else screens.normalize_placement(
            {k: body[k] for k in ("x", "y", "scale") if k in body})
    except ValueError as e:
        raise HTTPException(400, str(e))
    cfg = await asyncio.to_thread(state.mutate, lambda cfg: {
        "image_placements": {**cfg["image_placements"], a["file"]: {
            **cfg["image_placements"].get(a["file"], {}), layout: p}}})
    state.log_event("config", f"placed image {a['file']} in {layout} at "
                              f"{p or 'the automatic fit'}")
    m = await _rerender("placement") if _on_panel(a["file"]) else renderer.meta()
    return {"render": m, "current": _current(cfg), "animal": _animal_summary(a, cfg)}


@app.post("/api/mark-reviewed")
async def api_mark_reviewed(request: Request):
    """Mark an image reviewed: {file}. The review page sends it when it moves
    on from a print."""
    body = await request.json()
    if not isinstance(body.get("file"), str):
        raise HTTPException(400, "file is required")
    a = await _target_animal(body)
    cfg = await asyncio.to_thread(state.mutate, lambda cfg: {"reviewed_images": (
        cfg["reviewed_images"] if a["file"] in cfg["reviewed_images"]
        else cfg["reviewed_images"] + [a["file"]])})
    return {"animal": _animal_summary(a, cfg)}


REVIEW_DAYS = 28   # how far ahead the review queue runs


# The background fetch of upcoming animals the review page is waiting on.
_upcoming_fetch: asyncio.Task | None = None


def _fetch_upcoming_in_background(args: tuple) -> None:
    """Resolve the review page's missing slots off the request (a cold
    28-day fetch can outlast Cloudflare's 100 s timeout). One run at a
    time; the page re-polls, and a later poll starts another run for
    anything still missing."""
    global _upcoming_fetch
    if _upcoming_fetch and not _upcoming_fetch.done():
        return

    async def run():
        try:
            await asyncio.to_thread(datasources.fetch_upcoming, *args)
        except Exception as e:
            log.error("fetching upcoming animals failed: %s", e, exc_info=True)
    _upcoming_fetch = asyncio.create_task(run())


def _print_screen(cfg: dict) -> str:
    """The print screen the panel shows, or the dashboard while it shows a
    test pattern."""
    return cfg["screen"] if cfg["screen"] in screens.PRINT_SCREENS else "dashboard"


@app.get("/api/review")
async def api_review():
    """The review queue: the next REVIEW_DAYS days' animals in the order
    they will hang (datasources.upcoming_animals), each with its settings;
    `start` is where the queue opens, the first print not reviewed yet.
    `screen` is the print screen the panel shows and `layouts` the art
    layout each print screen places a print in at the panel's orientation.
    Answers at once from the cache: a slot not fetched yet is "pending" and
    is fetched in the background for the page's next poll."""
    cfg = state.load_config()
    args = (date.today(), REVIEW_DAYS, cfg["animal_offset"],
            cfg["animal_period_days"], cfg["vetoed_images"])
    try:
        items = await asyncio.to_thread(datasources.upcoming_animals, *args)
    except Exception as e:
        log.error("reading upcoming animals failed: %s", e, exc_info=True)
        raise HTTPException(500, f"could not read the upcoming animals: {e}")
    if any(it["pending"] for it in items):
        _fetch_upcoming_in_background(args)
    current = datasources.cached_animal()
    out = [{"dates": it["dates"], "pending": it["pending"],
            "on_panel": bool(it["animal"] and current
                             and it["animal"].get("file") == current.get("file")),
            "animal": _animal_summary(it["animal"], cfg) if it["animal"] else None}
           for it in items]
    animals = [o["animal"] for o in out if o["animal"]]
    size = renderer.upright_size(cfg["rotation"])
    layouts = {s: _layout_summary(screens.panel_layout({**cfg, "screen": s}, size))
               for s in screens.PRINT_SCREENS}
    start = next((i for i, o in enumerate(out)
                  if o["pending"] or (o["animal"] and not o["animal"]["reviewed"])), 0)
    return {"items": out, "start": start, "screen": _print_screen(cfg),
            "layouts": layouts, "total": len(animals),
            "reviewed": sum(1 for a in animals if a["reviewed"])}


PAGE_PREVIEW_MAX = 1200  # px, the long side of a whole-page preview or its layers


async def _previewed_animal(file: str, cfg: dict) -> dict:
    """The animal a preview draws, with its saved settings. 404 unless the
    file is on the panel or in the review queue."""
    a = await asyncio.to_thread(datasources.find_animal, file)
    if not a:
        raise HTTPException(404, "no such image on the panel or in the review list")
    return screens.with_image_settings(a, cfg)


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _webp_uri(img, lossless: bool) -> str:
    buf = io.BytesIO()
    img.save(buf, "WEBP", lossless=lossless, quality=85 if not lossless else 80)
    return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode()


def _preview_screen(screen: str | None, cfg: dict) -> tuple[str, tuple[int, int], str | None]:
    """A preview's screen (default: the panel's print screen; 400 unless it
    hangs a print), the panel's upright size and the art layout it uses
    (None: its print can't be placed)."""
    screen = screen or _print_screen(cfg)
    if screen not in screens.PRINT_SCREENS:
        raise HTTPException(400, f"screen must be one of {', '.join(screens.PRINT_SCREENS)}")
    size = renderer.upright_size(cfg["rotation"])
    return screen, size, screens.panel_layout({**cfg, "screen": screen}, size)


@app.get("/api/page-preview.png")
async def api_page_preview(file: str, screen: str | None = None):
    """The whole page `screen` (dashboard or daily; default: the panel's)
    will hang with this print, with its saved placement, in place of
    today's, at the panel's current orientation, drawn by the screen's own
    code (screens.page_preview) and shown as the panel will (dithered to the
    six inks). Headers: X-Placement (as drawn, clamped), X-Layout (the art
    layout), X-Art-Box (the art box as [x0, y0, x1, y1] shares of the
    page); X-Layout and X-Art-Box are left out for a page whose print
    can't be placed (the portrait daily view)."""
    cfg = state.load_config()
    screen, size, layout = _preview_screen(screen, cfg)
    animal = await _previewed_animal(file, cfg)

    def draw():
        img, box, placed = screens.page_preview(screen, animal, cfg, size)
        img = palette.simulate_panel(palette.dither_to_spectra(img))
        img.thumbnail((PAGE_PREVIEW_MAX, PAGE_PREVIEW_MAX))
        w, h = size
        share = box and [round(box[0] / w, 4), round(box[1] / h, 4),
                         round(box[2] / w, 4), round(box[3] / h, 4)]
        return _png(img), share, placed
    try:
        png, share, placed = await asyncio.to_thread(draw)
    except Exception as e:
        log.error("page preview failed for %s on %s: %s", file, screen, e, exc_info=True)
        raise HTTPException(500, f"preview failed: {e}")
    headers = {"Cache-Control": "no-store", "X-Placement": json.dumps(placed)}
    if layout:
        headers |= {"X-Layout": layout, "X-Art-Box": json.dumps(share)}
    return Response(png, media_type="image/png", headers=headers)


@app.get("/api/page-layers")
async def api_page_layers(file: str, screen: str | None = None):
    """The page `screen` would hang with this print (as page-preview draws
    it, undithered), split into layers for the pane and the review page to
    move and scale the print live under a finger (screens.page_layers,
    DECISIONS §29): "page" (everything but the art, transparent over it)
    and "art" (the plate, unclipped, with its "rect" and "centre") as
    data: URIs in the panel simulation's tones, "paper" (the colour under
    them), "box", "fades", "auto" and "placement" in frame px of "size",
    the panel's upright frame, and "limits" (the placement clamp). 400
    for a page whose print can't be placed (the portrait daily view)."""
    cfg = state.load_config()
    screen, size, layout = _preview_screen(screen, cfg)
    if layout is None:
        raise HTTPException(400, f"the {screen} page shows the print whole; it can't be placed")
    animal = await _previewed_animal(file, cfg)

    def draw():
        out = screens.page_layers(screen, animal, cfg, size, PAGE_PREVIEW_MAX)
        # In the panel simulation's tones, on its paper, like the picture
        # at rest (which is dithered too; the layers are not).
        out["page"] = _webp_uri(palette.simulate_tones(out["page"]), lossless=True)
        if out["art"]:
            out["art"]["image"] = _webp_uri(palette.simulate_tones(out["art"]["image"]),
                                            lossless=False)
        out["paper"] = "#%02x%02x%02x" % palette.simulate_tones(
            Image.new("RGB", (1, 1), (255, 255, 255))).getpixel((0, 0))
        return out
    try:
        out = await asyncio.to_thread(draw)
    except Exception as e:
        log.error("page layers failed for %s on %s: %s", file, screen, e, exc_info=True)
        raise HTTPException(500, f"layers failed: {e}")
    return JSONResponse(out | {"screen": screen, "layout": layout, "size": list(size),
                               "limits": {"shift": screens.PLACEMENT_SHIFT,
                                          "scale": list(screens.PLACEMENT_SCALE)}},
                        headers={"Cache-Control": "no-store"})


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
