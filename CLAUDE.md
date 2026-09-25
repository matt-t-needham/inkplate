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
| `screens.py` | Screen registry: `(width, height) -> PIL RGB image`, add to `SCREENS` dict, appears in the pane automatically. `dashboard` is the real screen — minimal layout: weather top-left, talking point along the bottom border, daily engraving as an edge-faded background bleeding off the right (`_edge_fade_mask`), caption top-right. Plate standardisation: `_trim_borders` → `_subject_box` (**projection-profile analysis, not an absolute bbox** — one stray mark stretches a bbox to the whole sheet, which is how the crop used to fail silently) → white point via `_paper_tone` (75th percentile, not median: on a densely inked plate the median pixel *is* ink). Figures are contained in a fixed panel-space art box, scenes cover it, and the fade is a fixed pixel width so it never varies with plate size. Fonts: Michroma display (`fonts/`, the rage channel font; use `_deg()` for degree marks — its own `°` is broken-looking), DejaVu Serif Italic for the question. White stroke halos keep text legible over the art. Icons are Weather Icons font glyphs (`WI_GLYPHS`, `_weather_icon`/`_glyph`): draw them **filled, never stroked** (line art doubles), except yellow which gets a thin black stroke for contrast; reference codepoints as `chr(0xf00d)` — literal PUA characters get stripped in transit |
| `datasources.py` | External data with disk-backed TTL caches + stale-on-error. Weather: Open-Meteo keyed by lat/lon. Daily animal: `COLLECTIONS` registry of seven historical corpuses on Commons (IZ backstop + Gessner/Buffon/Brehm/Catesby/Audubon/Gould), daily rotation with fall-through; **only files whose title names the beast are accepted** (full-text matches lie); anatomy-plate filter (Dutch/French/English tokens); `ANIMALS` is 228 (common, latin, native, kind) tuples, all verified to resolve against the live API — **re-run a resolution sweep after editing it** (see DECISIONS §20b); a two-word Latin term must match in full (genus alone mislabels: "Canis" catches jackals). Credit always describes the original work: files dated 1900+ are modern photos of a plate (usable, but preferred *after* real scans) and their Artist/date are ignored in favour of the corpus attribution. **Never render a placeholder** — a field naming nobody drops its clause instead of printing "Unknown"; an *absent* Artist falls back to the corpus, an explicitly-unknown one does not. rage.bix now-playing. Tests monkeypatch `_get_json`/`_get_bytes` — the suite never touches the network |
| `questions.py` | Curated talking-point bank (120, balanced 30 per category) + slot-seeded pick (no repeats within a full cycle). **House style is documented in the module docstring and enforced by tests** — casual, takes a side and invites a challenge; keep the categories even. `slot_for(day, period_days, offset)` is the shared content-slot helper both daily widgets key off — the animal cache is keyed by slot too |
| `renderer.py` | Orchestration + disk cache (`data/render/{raw,dithered,panel}.png` + `meta.json`); `ensure_fresh()` re-renders on staleness/screen change; serves stale frame if a render fails |
| `palette.py` | Spectra-6 palettes + Floyd-Steinberg dither (Pillow C core). `DITHER_PALETTE` order = firmware color indexes — don't reorder |
| `state.py` | `config.json` (atomic writes) + self-capping ndjson logs (`events`, `checkins`) |
| `static/index.html` | Single-file control pane, Catppuccin Mocha, no build step. **Relative URLs only** — served at both `:3008/` and `:9000/inkplate/` |
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
