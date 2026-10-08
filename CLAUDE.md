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
| `screens.py` | Screen registry: `(width, height) -> PIL RGB image`, add to `SCREENS` dict, appears in the pane automatically. `dashboard` is the real screen — minimal layout: weather top-left (left-aligned; today's *forecast* glyph + current °C with small °F + high, then sunrise/sunset, then the next two days by weekday abbreviation — never TODAY/TOMORROW), optional snow report (`_snow_report`: snowpack row, then a forecast-glyph row) stacked just above the quote, the daily quote (serif italic, ≤ 3 lines via `quote_fits`, attribution line "— Author, Work (Year)" beneath) along the bottom border, daily engraving as an edge-faded background bleeding off the right (`_edge_fade_mask`), caption top-right (name / native range / depiction each on their own line, wrapped at `CAPTION_WIDTH` so it clears the HIGH temperature). The snow block is labelled with `snow_location_name`. Plate standardisation: `_trim_borders` → `_subject_box` (**projection-profile analysis, not an absolute bbox** — one stray mark stretches a bbox to the whole sheet, which is how the crop used to fail silently) → white point via `_paper_tone` (75th percentile, not median: on a densely inked plate the median pixel *is* ink). Figures are contained in a fixed panel-space art box, scenes cover it, and the fade is a fixed pixel width so it never varies with plate size. Fonts: Michroma display (`fonts/`, the rage channel font; use `_deg()` for degree marks — its own `°` is broken-looking), DejaVu Serif Italic for the quote. White stroke halos keep text legible over the art. Icons are Weather Icons font glyphs (`WI_GLYPHS`, `_weather_icon`/`_glyph`): draw them **filled, never stroked** (line art doubles), except yellow which gets a thin black stroke for contrast; reference codepoints as `chr(0xf00d)` — literal PUA characters get stripped in transit |
| `datasources.py` | External data with disk-backed TTL caches + stale-on-error. Weather: Open-Meteo keyed by lat/lon. Daily animal: `COLLECTIONS` registry of seven historical corpuses on Commons (IZ backstop + Gessner/Buffon/Brehm/Catesby/Audubon/Gould), daily rotation with fall-through; **only files whose title names the beast are accepted** (full-text matches lie); anatomy-plate filter (Dutch/French/English tokens); `ANIMALS` is 228 (common, latin, native, kind) tuples, all verified to resolve against the live API — **re-run a resolution sweep after editing it** (see DECISIONS §20b); a two-word Latin term must match in full (genus alone mislabels: "Canis" catches jackals). Credit always describes the original work: files dated 1900+ are modern photos of a plate (usable, but preferred *after* real scans) and their Artist/date are ignored in favour of the corpus attribution. **Never render a placeholder** — a field naming nobody drops its clause instead of printing "Unknown"; an *absent* Artist falls back to the corpus, an explicitly-unknown one does not. Snow: Open-Meteo at its own lat/lon (`get_snow`: modelled depth, 72 h snowfall, 3 days) — a grid-cell estimate, not the resort's reported base. rage.bix now-playing. Tests monkeypatch `_get_json`/`_get_bytes` — the suite never touches the network |
| `quotes.py` | Daily quote from Wikiquote's Quote of the Day (MediaWiki `action=parse`). **Attribution and year are mandatory**: the quote must be found on the author's page outside Disputed/Misattributed/Attributed/"about" sections and a year must come from its citation or section heading — otherwise the candidate is skipped, never shown bare. Fallbacks are seeded random archive dates strictly before `NO_REPEAT_BEFORE` (2026-10-07); a shown-history cache prevents repeats; vetoes live in config `vetoed_quotes`. Tests fake the API via `datasources._get_json` |
| `renderer.py` | Orchestration + disk cache (`data/render/{raw,dithered,panel}.png` + `meta.json`); `ensure_fresh()` re-renders on staleness/screen change; serves stale frame if a render fails |
| `palette.py` | Spectra-6 palettes + Floyd-Steinberg dither (Pillow C core). `DITHER_PALETTE` order = firmware color indexes — don't reorder |
| `state.py` | `config.json` (atomic writes) + self-capping ndjson logs (`events`, `checkins`). Battery: `battery_percent` (Li-ion voltage curve) — shown in the pane only, deliberately never on the panel |
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
- With `show_now_playing` on, `ensure_fresh` uses the fast
  `NOW_PLAYING_RENDER_TTL_S` staleness window instead of `refresh_minutes` —
  renders are cheap; device redraws still gate on the hash.
- "Push" is a version bump the device polls for; a deep-sleeping device is
  unreachable by design. Tethered mode (config flag) = fast polling for dev.
- The pane is LAN-only and unauthenticated on purpose; don't add it to the
  Cloudflare tunnel without putting Access in front.
- Renders happen lazily on request (`ensure_fresh`), so the first hit after a
  cadence window pays ~150 ms — fine; don't add a background render loop
  without a reason.
