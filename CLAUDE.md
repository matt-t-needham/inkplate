# CLAUDE.md — inkplate_dash

> See also: [README](README.md) · [DECISIONS](DECISIONS.md) · [docs/esp32-primer.md](docs/esp32-primer.md)

## Project overview

HTPC-side feeder for the Inkplate 13SPECTRA (13″ six-color e-paper, ESP32-S3,
1600×1200). Server renders → dithers → serves frames; the device is a dumb
poller that wakes, downloads a PNG, draws, reports telemetry, deep-sleeps.
**The device may not have arrived yet** — firmware is unverified on hardware;
`VERIFY-ON-ARRIVAL` comments in the sketch mark the risky calls.

Module map:

| Module | Role |
|---|---|
| `main.py` | FastAPI app — control-pane API + device API. Renders via `asyncio.to_thread` under one lock |
| `config.py` | All tunables/env vars (`INKPLATE_DATA_DIR`, panel size, cadence bounds) |
| `screens.py` | Screen registry: `(width, height) -> PIL RGB image`, add to `SCREENS` dict, appears in the pane automatically. Screens are size-agnostic and get `1200x1600` when the panel is rotated to portrait. `dashboard` is the real screen, built from shared widgets (`_draw_weather`, `_draw_caption`, `_draw_art`, `_snow_report`) laid out per orientation — landscape, or one of `PORTRAIT_LAYOUTS` chosen by config `portrait_layout` (`hero`, the default and the primary hanging: a centred composition — engraving centred on its ink centroid with a soft `HERO_FADE` edge, caption overlaid top-right, then `_draw_weather_centred` (temperature row centred; forecast block left and sunrise-over-sunset block right, hugging ±`HERO_BAND_HALF`) and `_snow_foot` (one centred row along the bottom border); with snow off the band drops to the border and the art grows; anything laid over the art is measured first and the art lightened under it with a feathered white patch (`_fade_behind`); `stack`: info on top, engraving below). Minimal layout: weather top-left (left-aligned; today's *forecast* glyph + current °C with small °F + high, then sunrise/sunset, then the next two days by weekday abbreviation — never TODAY/TOMORROW), optional snow report (`_snow_report`: snowpack row, then a forecast-glyph row) bottom-left, daily engraving as an edge-faded background bleeding off the right (`_edge_fade_mask`), caption top-right (name / native range / depiction each on their own line, wrapped at `CAPTION_WIDTH` so it clears the HIGH temperature; in portrait it tucks *under* the HIGH column at `PORTRAIT_CAPTION_TOP` because the weather's first row spans most of a 1200px frame). The art box runs to the bottom border (the daily quote that used to sit there was removed 2026-10-16 — DECISIONS §22). The snow block is labelled with `snow_location_name`. Plate standardisation: `_trim_borders` → `_subject_box` (**projection-profile analysis, not an absolute bbox** — one stray mark stretches a bbox to the whole sheet, which is how the crop used to fail silently) → white point via `_paper_tone` (75th percentile, not median: on a densely inked plate the median pixel *is* ink). Figures are contained in a fixed panel-space art box then leaned into by `ART_ZOOM` (the overflow, cut around the ink centroid, is paper margin the subject box kept), scenes cover it, and the fade is a fixed pixel width so it never varies with plate size. A plate scanned sideways is turned by its per-image `rotation` (config `image_rotations`, pane **Rotate ↻**) before anything looks at it — there is no auto-detection, because the direction of the turn is a judgement the heuristics get wrong. Fonts: Michroma display (`fonts/`, the rage channel font; use `_deg()` for degree marks — its own `°` is broken-looking), DejaVu Serif Italic for the caption. White stroke halos keep text legible over the art. Icons are Weather Icons font glyphs (`WI_GLYPHS`, `_weather_icon`/`_glyph`): draw them **filled, never stroked** (line art doubles), except yellow which gets a thin black stroke for contrast; reference codepoints as `chr(0xf00d)` — literal PUA characters get stripped in transit |
| `datasources.py` | External data with disk-backed TTL caches + stale-on-error. Weather: Open-Meteo keyed by lat/lon. Daily animal: `COLLECTIONS` registry of seven historical corpuses on Commons (IZ backstop + Gessner/Buffon/Brehm/Catesby/Audubon/Gould), daily rotation with fall-through; **only files whose title names the beast are accepted** (full-text matches lie) — a genus-only Latin term must *lead* the binomial and the common name must be a whole word, never a substring ("Papilio" caught the fish *Pteraclis papilio*; "butterfly" catches butterflyfish); vetoes are per Commons file (`vetoed_images`, keyed by the cached animal's `file`), never per animal; anatomy-plate filter (Dutch/French/English tokens); `ANIMALS` is 227 (common, latin, native, kind) tuples, all verified to resolve against the live API — **re-run a resolution sweep after editing it** (see DECISIONS §20b, §20e); a two-word Latin term must match in full (genus alone mislabels: "Canis" catches jackals). A period genus that modern taxonomy has handed to a different beast is the other mislabel ("Cynocephalus" is the colugo now, so the baboon ran as a flying lemur) — prefer the genus the corpus files the animal under today. Credit always describes the original work: files dated 1900+ are modern photos of a plate (usable, but preferred *after* real scans) and their Artist/date are ignored in favour of the corpus attribution. **Never render a placeholder** — a field naming nobody drops its clause instead of printing "Unknown"; an *absent* Artist falls back to the corpus, an explicitly-unknown one does not. Snow: Open-Meteo at its own lat/lon (`get_snow`: modelled depth, 72 h snowfall, 3 days) — a grid-cell estimate, not the resort's reported base. Tests monkeypatch `_get_json`/`_get_bytes` — the suite never touches the network |
| `renderer.py` | Orchestration + disk cache (`data/render/{raw,dithered,panel,frame}.png` + `meta.json`); `ensure_fresh()` re-renders on staleness/screen/rotation change; serves stale frame if a render fails. **Rotation lives here**: the screen is drawn upright at `upright_size(rotation)` and the three previews stay upright (what's on the wall); only `frame.png` — what the device downloads — is turned by `to_panel_frame`, so the firmware always gets native 1600×1200. `rotation` is the panel's clockwise turn from landscape (90 = portrait); PIL's `rotate()` is anticlockwise, which is exactly the inverse the frame needs |
| `palette.py` | Spectra-6 palettes + Floyd-Steinberg dither (Pillow C core). `DITHER_PALETTE` order = firmware color indexes — don't reorder |
| `state.py` | `config.json` (atomic writes; `rotation` ∈ `ROTATIONS`, `portrait_layout`, `image_rotations` {Commons file → 90/180/270} validated here) + self-capping ndjson logs (`events`, `checkins`). Battery: `battery_percent` (Li-ion voltage curve) — shown in the pane only, deliberately never on the panel |
| `static/index.html` | Single-file control pane, Catppuccin Mocha, no build step. **Relative URLs only** — served at `:3008/` (LAN, device + pane) and `inkplate.bix.computer` (tunnel, behind Access) |
| `firmware/inkplate_dash/` | Arduino sketch + `config.h.example` (real `config.h` is gitignored — Wi-Fi creds) |

## Device protocol (firmware ↔ server contract)

- `GET /api/display/meta` → `{image_version, sleep_seconds, tethered}` — cheap
  version probe; the device redraws **only** when `image_version` differs from
  what it stored in RTC memory (a redraw = ~20 s of flashing + battery).
- `GET /api/display.png` → indexed PNG containing exactly the six palette
  colors; headers `X-Image-Version`, `X-Sleep-Seconds`.
- `POST /api/device/checkin` ← telemetry JSON; response repeats the meta so
  cadence/tethered changes propagate every wake.

Changing any of these shapes means reflashing the physical device — treat the
contract as frozen once hardware is in the field; extend additively.

## How to run / verify

```bash
.venv/bin/python -m pytest -q                       # 27 offline tests, ~1s
docker build --target test .                        # what the deploy gate runs
# Deploy (from bix-infra/): docker compose build inkplate && docker compose up -d inkplate
```

## Gotchas

- **Container rebuild required for any change** (static/ is COPY'd, no mount).
- `data/` is a bind mount — config and renders survive rebuilds; deleting it
  resets to defaults harmlessly (and re-fetches today's animal/weather).
- **Versioning is content-hash based** (`renderer.render`): `image_version`
  bumps only when the dithered pixels change (or on force). Don't put a
  minutes-precision clock on a screen — it would defeat the whole mechanism
  and burn a 20s panel refresh every render.
- Rotating the panel is a server-side config change, never a firmware one:
  the device contract stays 1600×1200. A rotation change bumps the version
  (the frame hash changes) so the device redraws at its next wake.
- Layout previews rendered with synthetic plates live in `docs/layouts/`
  (Commons was unreachable where they were made); regenerate from a real
  render via the pane when judging art placement.
- "Push" is a version bump the device polls for; a deep-sleeping device is
  unreachable by design. Tethered mode (config flag) = fast polling for dev.
- The pane is LAN-only and unauthenticated on purpose; don't add it to the
  Cloudflare tunnel without putting Access in front.
- Renders happen lazily on request (`ensure_fresh`), so the first hit after a
  cadence window pays ~150 ms — fine; don't add a background render loop
  without a reason.
