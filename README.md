# inkplate_dash

Server-side feeder for the **Inkplate 13SPECTRA** — a 13″, 1600×1200, six-color
e-paper board arriving in the mail. The HTPC does all the work: it renders the
dashboard as an image, dithers it to the panel's six inks, and serves it over
LAN. The device just wakes up, downloads the frame, shows it, phones home, and
goes back to deep sleep.

**Status: content live, hardware pending.** The pipeline is complete and
tested end-to-end, and the `dashboard` screen renders real content. The
firmware is written but has never touched hardware (the device hasn't
arrived).

## The dashboard

Four widgets, each failing independently (a dead API degrades one box, never
the panel):

- **Weather** (Open-Meteo, keyless) — current temp in °C and °F, condition
  icons from [Weather Icons](https://erikflowers.github.io/weather-icons/)
  (eleven states: sun, partly, cloud, fog, drizzle, rain, showers, sleet,
  snow, storm, hail), today's sunrise/sunset, and three days with low on the
  left and high on the right in both units. Location is set in the control
  pane (defaults to the HTPC's IP geolocation).
- **Talking point** — a daily question from the curated bank in
  `questions.py` (personal, philosophy, politics, ethics: moral / medical /
  computing / global). Date-seeded: same question all day, no repeats until
  the whole bank has cycled (~4 months). Edit the bank freely.
- **Animal engraving** — a daily historical animal print from Wikimedia
  Commons, rotating across seven corpuses: Iconographia Zoologica (Amsterdam,
  1700-1880, the reliable backstop), Gessner's 1551 *Historiae animalium*,
  Buffon (Paris, 1749), Brehm (Leipzig, 1863), and — birds — Catesby (1731),
  Audubon (1827), Gould (1840). 228 animals with native ranges — land, air
  and a deep ocean section — every one verified to resolve; deterministic
  daily pick; auto-cropped, paper cleaned, drawn as a soft edge-faded
  background. Caption: *"Rhinoceros · Rhinoceros indicus · of South Asia · as
  depicted by Conrad Gessner, Zürich, 1551"*. Falls back to yesterday's beast
  if Commons is down.
- **Now playing** (optional, off by default) — the current track on the RAGE
  channel from the "Whats Playing?" service. Only sensible on mains power
  with a short cadence: a deep-sleeping device can't be told a song changed.
  Thanks to content-hash versioning (below) enabling it doesn't waste
  refreshes — the panel only redraws when the pixels actually changed.

**Content-hash versioning:** `image_version` bumps only when the rendered
frame's pixels differ from the previous render. The device does a ~100-byte
version check each wake and skips the ~20s flashing refresh entirely when
nothing changed. "Render now → push" in the pane force-bumps (guaranteed
redraw — the recovery path for a garbled panel).

## Why this shape

```
┌─────────────────────── HTPC ────────────────────────┐      ┌── Inkplate 13SPECTRA ──┐
│  screens.py ──► renderer.py ──► palette.py          │      │  wake from deep sleep  │
│  (draw RGB)     (cache PNGs)    (dither to 6 inks)  │      │  GET /api/display/meta │
│                                                     │◄─────│  GET /api/display.png  │
│  control pane (:3008 or metrics:9000/inkplate/)     │      │  draw · POST check-in  │
│  config.json · events.ndjson · checkins.ndjson      │      │  deep sleep (~14 µA)   │
└─────────────────────────────────────────────────────┘      └────────────────────────┘
```

All intelligence lives on the server, so changing the dashboard never means
re-flashing the device — iterate in Python, hit "Render now", done. See
[DECISIONS.md](DECISIONS.md) for every choice made and why, and
[docs/esp32-primer.md](docs/esp32-primer.md) if ESP32/microcontrollers are new
territory (they were — that doc is the crash course).

## Running it

```bash
# Dev (outside Docker)
cd inkplate_dash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn main:app --reload --port 8000

# Tests
.venv/bin/pip install pytest==8.3.4
.venv/bin/python -m pytest -q

# Production (via Docker Compose in bix-infra/) — test suite gates the build
cd bix-infra
docker compose build inkplate && docker compose up -d inkplate
docker logs apps-inkplate-1 -f
```

- Control pane: `http://<server-ip>:3008` · also linked from the metrics page
  sidebar at `http://<server-ip>:9000/inkplate/`
- Logs: `bix-infra/logs/inkplate/app.log` (rotating 10MB×3)
- State: `inkplate_dash/data/` (bind mount — survives rebuilds)

## The control pane

- **Preview** — exactly what the panel will show. Three views: *panel sim*
  (muted pigment colors, closest to real e-paper), *dithered* (the actual
  pixels the device gets), *raw* (pre-dither RGB).
- **Render now → push** — re-renders and bumps the content version. The device
  redraws at its next wake; in tethered mode that's within seconds.
- **Next animal / Next question** — advance the daily sequences for layout
  testing. Persisted as offsets, so the preview and the device stay in sync.
- **Configuration** — screen picker, refresh cadence (how often the panel
  redraws — keeps the temperature current), separate animal and question
  cadences in days (default daily), location, tethered mode. Applies live;
  the device picks up cadence changes at its next check-in.
- **Device check-ins** — battery voltage, Wi-Fi signal, boot count, errors.
  The header badge turns red if the device misses ~2.5 wake windows.
- **Server events** — render/config/error trail, so debugging needs no shell.

Note on "push": a deep-sleeping device is unreachable (radio off, ~14 µA) —
nothing can be pushed *to* it. "Render now" stages the update; the device pulls
it on its own schedule. For instant iteration, flip on **tethered mode**: the
device stays awake and polls every few seconds. Use it at the desk, not on
battery.

## Adding more dashboard content

1. Write a function in `screens.py` that takes `(width, height)` and returns a
   Pillow RGB image (see `dashboard` for the pattern — data via
   `datasources.py` cached fetchers, degrade gracefully).
2. Add it to the `SCREENS` dict.
3. It appears in the control pane's screen picker. Check it in *panel sim*
   preview — six inks is not many; bold shapes and dithered mid-tones work,
   subtle gradients don't.

---

# 📦 When the device arrives — day-one checklist

Work through this in order. Steps 1–4 need no firmware at all.

### 1. Unbox sanity check
- Plug in via **USB-C**. It should power on and likely show a factory demo.
- If it shipped with a battery, connect it to the **JST** connector (check
  polarity against Soldered's docs before plugging — JST polarity is not
  universal).

### 2. Confirm the server is up (nothing to install)
- Open the control pane, hit **Render now**, confirm the preview updates.
- From any LAN machine: `curl -I http://<server-ip>:3008/api/display.png` →
  `200` with `X-Image-Version` and `X-Sleep-Seconds` headers.

### 3. Set up the Arduino IDE (~20 min, one time)
1. Install [Arduino IDE 2.x](https://www.arduino.cc/en/software).
2. **File → Preferences → Additional boards manager URLs**: add Soldered's
   Dasduino/Inkplate board package URL (it's in the Inkplate 13SPECTRA docs at
   soldered.com — they pledged docs + examples at delivery).
3. **Boards Manager**: install the Soldered ESP32 boards package; select the
   **Inkplate 13SPECTRA** board.
4. **Library Manager**: install **InkplateLibrary** (Soldered) and
   **ArduinoJson** (Benoit Blanchon).
5. Open one of the library's own 13SPECTRA examples (image-from-web or Wi-Fi
   example) and flash it first. Getting a *vendor* example working proves the
   toolchain before our code enters the picture. On Linux you may need:
   `sudo usermod -aG dialout $USER` (then log out/in) for serial port access.

### 4. Flash inkplate_dash firmware
1. `cp firmware/inkplate_dash/config.h.example firmware/inkplate_dash/config.h`
   and fill in Wi-Fi credentials. `SERVER_URL` should already be right.
2. Open `firmware/inkplate_dash/inkplate_dash.ino` in the IDE.
3. Search the sketch for **VERIFY-ON-ARRIVAL** comments — each marks an API
   call to double-check against the library's real 13SPECTRA examples
   (`drawImage` signature, `readBattery`, panel power-down before sleep).
   Fix any that differ; they're all one-liners.
4. Flash. Open **Serial Monitor at 115200 baud** — the sketch narrates every
   step (wifi ok → new frame → drawing → sleeping).

### 5. Bring-up, in this order (each step isolates one failure mode)
1. In the control pane, enable **tethered mode** first — no deep sleep, fast
   polls, everything observable.
2. Set screen to **alignment** → checks geometry (border visible on all four
   edges? crosshair centered? not rotated/mirrored?).
3. Set screen to **palette_test** → six bands, right colors, right order. If
   colors are swapped, the PNG color→ink mapping needs adjusting in firmware.
4. Set screen to **dither_test** → gradients should look like smooth-ish
   newsprint, not banding or garbage.
5. Watch check-ins appear in the control pane: battery voltage and RSSI sane?
6. Turn **tethered off**, set cadence to 60 min, unplug USB → confirm a
   check-in arrives each hour and the header badge stays green.

### 6. Known unknowns to confirm on real hardware
- Actual full-refresh time (spec says ~20 s) and how intrusive the flashing is.
- Real battery drain per wake cycle vs the ~40–50 day claim.
- Whether the 8 s Wi-Fi connect estimate holds on our network (WPA3 or a weak
  signal can slow it — check RSSI in the pane; consider a static IP lease for
  the device to skip DHCP).
- PNG decode: 1600×1200 indexed PNG must fit ESP32-S3 decode buffers — with
  16 MB PSRAM it should be trivial, but if `drawImage` chokes, the fallback is
  serving raw uncompressed bitmap format instead (server change, small).
