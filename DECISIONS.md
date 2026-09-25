# Decision log

Decisions made autonomously while scaffolding (2026-09-17), with the
reasoning. Anything here is revisitable — nothing is load-bearing enough that
changing it later is expensive, except where noted.

## 1. Dumb device, smart server (the big one)
**Chosen:** the HTPC renders complete, pre-dithered frames; the device is a
generic "download PNG, show it, sleep" poller.
**Over:** rendering on the device (drawing text/shapes in firmware), or
ESPHome-managed rendering.
**Why:** every dashboard iteration becomes a Python edit + browser refresh
instead of a USB flash cycle. Firmware converges to a tiny, stable program you
flash once or twice ever. Also sidesteps the riskiest unknown — a brand-new
board whose library may be immature at delivery — by using only its most basic
capability (draw an image). The 16 MB PSRAM makes full-frame PNG handling
comfortable.

## 2. Arduino/C++ firmware, not MicroPython or ESPHome
**Why Arduino:** it's Soldered's primary supported path (their examples,
docs, and library land there first); the poller is ~200 lines and barely
changes, so the C++ ergonomics tax is paid once; deep-sleep power behavior is
most predictable there.
**Why not MicroPython:** second-class support at launch for a new board, and
firmware simplicity means Python's iteration advantage buys little here — the
iteration all happens server-side.
**Why not ESPHome:** it was only *pledged* for delivery, and its model
(device renders widgets from YAML) fights decision #1. Worth a look later if
you want Home-Assistant-native sensors, but not as the display path.
**Why not a different board entirely** (Pi Zero + panel, etc.): the panel is
the product here; a Pi burns ~100 mW idle (days of battery, not months), and
the ESP32-S3 with 16 MB PSRAM is generously specced for a poller. There is no
better-suited architecture for battery e-paper. (See docs/esp32-primer.md for
the ESP32 background.)

## 3. Pillow rendering, not headless-browser HTML screenshots
**Chosen:** screens are Python functions drawing with Pillow.
**Why:** zero heavy deps (a headless Chromium is ~400 MB and its own failure
domain), deterministic output, trivially testable offline, and e-paper layouts
want big/bold/exact drawing rather than CSS flow anyway.
**Cost:** no CSS niceties. If a future screen really wants HTML, add a
renderer that screenshots via Playwright *as another screen function* — the
registry doesn't care where pixels come from. Revisit-cost: low.

## 4. Pre-dithering on the server, Floyd-Steinberg, saturated palette
The server quantizes to exactly six colors (Pillow's C-core FS dither,
~150 ms/frame). The device does **no** pixel processing — `drawImage(...,
dither=false)`. Dither targets are the saturated ideals (pure red/green/…)
that vendor tooling assumes; the *preview* additionally offers a muted
"panel sim" remap because real pigment is duller than RGB, and judging layouts
against neon colors misleads. If the panel's real response makes some ink
render poorly, adjust `palette.DITHER_PALETTE` weights server-side — firmware
untouched.

## 5. Poll, don't push — and "tethered mode" for development
A deep-sleeping ESP32 has its radio off; nothing can reach it, so true push is
physically impossible in the battery-powered design. "Render now → push" in
the pane = render + version bump, picked up at the device's next wake.
For bring-up and rapid iteration, **tethered mode** (a config flag the device
reads at every check-in) keeps it awake polling every few seconds — push-like
latency while on USB power. This is the honest version of every e-paper
project's update story.

## 6. Version-gated redraws
The device stores the last-drawn `image_version` in RTC memory and does a
~100-byte `GET /api/display/meta` before deciding to download/redraw. A
Spectra full refresh is ~20 s of visible flashing and the single biggest
battery cost — unchanged content must never trigger one. This also means the
hourly render cadence and the device wake cadence can drift independently
without waste.

## 7. Cadence is server-owned
The device is told how long to sleep in every response (`sleep_seconds`).
Changing cadence in the pane needs no reflash; a device that misses the memo
simply picks it up next wake. Bounds enforced server-side: 1 min–24 h.

## 8. FastAPI service on port 3008, compose-managed, test-gated Docker build
Follows the house pattern exactly (bix-demucs structure, bix-ai's
test-gate Dockerfile). 3008 is used (3007 in the map was free on paper but an unmanaged "Whats Playing?" host service already held it). Logs rotate to
`bix-infra/logs/inkplate/app.log`; mutable state is a bind mount of
`inkplate_dash/data`. LAN-only — not added to the Cloudflare tunnel: the
device and the pane both live on the LAN, and the pane is unauthenticated by
design (exposing it publicly would need Cloudflare Access first).

## 9. Metrics-page integration via nginx proxy
The metrics site (:9000) gets a `location /inkplate/` proxying to the
container over the `bix` docker network, and a sidebar link (same pattern as
the Steam control page). The pane uses only relative URLs so it works
identically at `:3008/` and `:9000/inkplate/`. Chosen over embedding panels
into `generate_metrics.py` itself — the pane is live/interactive and the
metrics page is a static hourly regeneration; an iframe/proxy boundary keeps
them decoupled.

## 10. State on disk as JSON/ndjson, no database
`config.json` (atomic tmp+replace) plus self-capping ndjson logs for events
and device check-ins. Volumes match the workload: one writer, tiny records,
newest-N reads. Same reasoning as bix-demucs's file-based job tracking.

## 11. Timezone pinned to America/Los_Angeles
The container gets `TZ=America/Los_Angeles` (read from the host's
/etc/timezone) so on-panel timestamps are local. A dashboard that's wrong
about what time it is looks broken even when it isn't.

## 12. Firmware written blind, flagged honestly
The sketch targets the documented Soldered Inkplate API but the 13SPECTRA is
a new board — every call most likely to differ is marked `VERIFY-ON-ARRIVAL`
in the sketch and listed in the README checklist, rather than pretending the
code is known-good. Wi-Fi credentials live in a gitignored `config.h`.

## 13. (superseded by #14) Placeholder + three test screens now, content later
Per the brief, no real content yet. But the test screens (alignment, palette,
dither) aren't filler — they're the bring-up instruments for day one, each
isolating one class of hardware fault (geometry, color mapping, tone
rendering).

---

*Added 2026-09-17, second pass — the real dashboard content.*

## 14. The dashboard screen: weather + talking point + animal print + optional now-playing
Four widgets, each degrading independently (a dead API shows an "unavailable"
box or yesterday's data, never a blank panel). All data flows through
`datasources.py`: disk-cached, TTL'd, stale-on-error.

## 15. Weather from Open-Meteo
Keyless, free for non-commercial use, one call returns current conditions +
3-day min/max/sunrise/sunset in the location's own timezone — and it's the
API the Inkplate vendor's own examples use. Icons are drawn with Pillow
primitives (sun/cloud/rain/snow/storm/fog from WMO codes) — six flat inks
suit drawn shapes far better than any icon font. Location defaults to the
HTPC's IP geolocation (Portland, OR) and is editable in the pane.

## 16. Talking points from a curated local bank, not an API
~120 questions written into `questions.py` (personal / philosophy / politics /
ethics: moral, medical, computing, global). Online question APIs are flaky
and icebreaker-grade; a local bank is editable, reviewable, and free of a
failure mode. Selection is date-seeded: deterministic within a day, no
repeats until the full bank cycles (~4 months). Refreshes daily as asked.

## 17. Animal engravings from Wikimedia Commons' Iconographia Zoologica
A ~25k-print collection of 19th-century zoological engravings (University of
Amsterdam) — exactly the requested genre, keyless API, thumbnail rendering
included. Daily deterministic pick from ~60 curated animals (searched by
Latin genus — filenames are Latin binomials; common-name search once returned
a pigeon for "owl"). Scans are full sheets, so `_prep_engraving` auto-crops
to the printed figure (5% inset + dark-pixel bbox, guarded) and stretches an
adaptive white point so aged paper dithers to clean white. Rejected: Met/
Cleveland APIs (thin, coin-polluted results), BHL/Smithsonian (API keys).

## 18. Now-playing (rage.bix) implemented but OFF by default — and the fix
that makes it feasible at all: **content-hash versioning**. `image_version`
now bumps only when the dithered pixels actually change, so the device —
which redraws only on a version change — never wastes a ~20s flashing refresh
on identical content. With now-playing enabled, renders go stale every
`NOW_PLAYING_RENDER_TTL_S` (120s) instead of the cadence, but the device only
redraws when the song actually changed. It's still only sensible on mains
power with a short device cadence (on battery at 60min the song is stale on
arrival — as the original brief suspected). Feed: the unmanaged
"Whats Playing?" service's `/api/now?channel=2` on :3007.

## 19. "Render now → push" stays force-bump
With hash versioning, a forced bump redraws even identical content — kept
deliberately as the recovery path for a garbled panel.

## 20a. Multi-corpus animal art (2026-09-17, fourth pass)
One collection meant one visual voice. `COLLECTIONS` in `datasources.py` now
rotates daily across seven historical corpuses on Commons: Iconographia
Zoologica (Amsterdam, 1700-1880 — the backstop: its filenames are binomials,
so it titles every animal), Gessner's *Historiae animalium* (Zürich, 1551 —
the drawn-from-a-traveler's-tale genre), Buffon (Paris, 1749), Brehm
(Leipzig, 1863), and for birds Catesby (England drawing the Americas, 1731),
Audubon (America, 1827) and Gould (London, 1840). The animal list grew to
~85 with a `kind` field (birds-only corpuses gate on it) — Amazon/tropical
birds, platypus, narwhal, and other early-depiction bait included.
**Correctness rule learned the fun way:** a full-text search match is not a
depiction — a Gessner page of sea monsters *mentions* rhinos. Only files
whose *title* names the beast are accepted; a collection without one is
skipped for that day. The caption carries the whole story without labeling
the joke: "Rhinoceros · Rhinoceros indicus · of South Asia · as depicted by
Conrad Gessner, Zürich, 1551". Anatomy-plate filter extended to French and
English tokens (Buffon's beaver-gland diagrams, no thank you).

## 20g. Standardising wildly different plates (tenth pass)
Plates arrive at 6× the spread in subject aspect ratio (0.26 for a seahorse,
1.53 for a crocodile), some are isolated figures on blank paper, some are
full-bleed landscapes, and most carry captions, marginalia and near-black
scan edges. Three separate faults were stacked on top of each other:

1. **The subject crop was silently failing on ~40% of plates.** It used an
   absolute bounding box of dark pixels, which any stray mark drags to the
   sheet edge; the >88% guard then rejected the crop and displayed the whole
   sheet. That, not size, is why the peacock looked small and faded early.
2. **The fade was computed in image space**, so 30% of a narrow plate and 30%
   of a wide one were different distances on the panel.
3. **No safe area**: the box was 1150px tall on a 1200px panel, so a portrait
   plate ran under the caption — the heron's head.

Fixed with the standard CMS pipeline (trim → smart crop → fixed box):
- `_trim_borders` strips near-solid dark scan edges *first*; they are denser
  than any figure, so a content search locks onto them otherwise (the
  crocodile came back as a black sliver until this existed).
- `_subject_box` uses **projection-profile analysis** — the figure is the
  densest contiguous band of ink on each axis, captions and marginalia are
  thin sparse ones. This is what an absolute bbox cannot distinguish. Verified
  on a 14-plate contact sheet; it also handles the two-figures-on-one-sheet
  case (macaw) by keeping both.
- A **fixed art box in panel coordinates** with the caption above and the
  talking point below. Figures are *contained* (never cut), scenes *cover*.
- The **fade is a fixed pixel width** on the panel, independent of the plate.
- `_paper_tone` estimates paper from the **75th percentile**, not the median:
  on a densely inked plate the median pixel is ink, which inverts the estimate.

Scene-vs-figure is decided by ink coverage, threshold set from measurement
rather than taste: single figures top out near 39% (a heron filling its
sheet), true scenes run 60%+ (a forest engraving), so the line sits at 50%.

## 20f. Question rewrite, and never print a placeholder (ninth pass)
**Questions** were rewritten to a house style now recorded at the top of
`questions.py`: casual and spoken, and where possible *taking a side and
inviting the reader to knock it down* ("X is true — convinced?") rather than
asking neutrally. The old set read like a seminar handout. Categories were
also rebalanced: ethics had drifted to 54 entries against politics' 18, so
ethics came up nearly half the time. Now 30 each, 120 total (~4 months
before anything repeats). Two tests guard this — one on the balance, one on
banned tone markers — because both problems creep back silently.

**Placeholders are never displayed.** A caption read "as depicted by Unknown
authorUnknown author, London" — two separate faults: Commons nests the
Artist field in markup that collapsed into a doubled string, and a large
share of plates are credited to "Unknown author" in the first place. Tags now
become spaces (not nothing) before collapsing whitespace, a doubled value is
halved, and — the general rule — **a field that names nobody prints nothing
at all**, never the word "Unknown". There is a deliberate distinction: an
*absent* Artist field falls back to the corpus attribution (a real fact about
where the plate came from), while a field that positively states the artist
is unknown drops the clause entirely rather than substituting a guess.

Layout: each day column now reads glyph → day name → temperatures, and the
first two columns are labelled TODAY and TOMORROW.

## 20e. Monochrome icons, 7.5% bleed, and crediting the original (eighth pass)
Icons are now drawn in a single ink (`ICON_INK`, black). Spectra has no greys,
so "grayscale" here means black — and coloured icons competed with the
engraving behind them. The artwork sits with its last 7.5% running off the
right border.

**Attribution bug found while checking whether any plates are in colour.**
Commons holds modern *photographs* of these plates (someone's shot of a
library book) next to the plate scans, and credits the photographer — so a
2011 Flickr user appeared in the caption as the 1840s illustrator. A
photograph is fine if it is well framed; the credit is what matters. So:
files whose `DateTimeOriginal` is 1900 or later are treated as
reproductions — still usable, and preferred *after* genuine plate scans, but
their Artist and date are ignored in favour of the collection's original
attribution. The caption always describes the work, never the digitiser.

**Are the plates in colour?** Measured, not assumed — sampling the inked
pixels (ignoring paper) across the corpuses: Audubon, Gould and Catesby are
hand-coloured and strongly so (channel spread 69–145), Iconographia Zoologica
is lightly tinted (≈50), Brehm is pure monochrome wood engraving (0). The
Spectra-6 palette renders the hand-coloured bird plates surprisingly well —
a Gould toucan keeps its orange bill.

## 20d. Weather Icons font replaces the hand-drawn set (seventh pass)
Swapped the drawn icons for **Weather Icons** by Erik Flowers (SIL OFL 1.1),
vendored in `fonts/` so the image builds offline. Bake-off findings that are
worth keeping:
- These are monochrome **line-art** glyphs, so they must be drawn *filled,
  never stroked-and-filled* — stroking line art doubles every outline and
  looks muddy. The one exception is yellow, which needs a thin black stroke
  to hold up on pale e-paper.
- Colour still carries meaning: yellow sun/lightning, blue for anything
  falling out of the sky, black for cloud and fog. White fills vanish on the
  paper ground — never use them for a glyph.
- The codepoints are Private Use Area. Write them as `chr(0xf00d)`; pasted
  literal PUA characters get silently stripped by some pipes (this cost a
  confusing blank render during the bake-off).

The font also supplies the sunrise/sunset glyphs, which replaced the drawn
ones — but they differ only by a chevron under the sun, invisible below about
70px, so that row is laid out from measured glyph widths at a deliberately
large size. The richer glyph set let `icon_kind` grow from 7 buckets to 11
(drizzle, showers, sleet and hail are now distinct from plain rain/snow).
All the hand-drawn shape machinery (`_union`, lobe tables, bolt polygons) is
gone — about 100 lines deleted.

## 20c. Icon redraw, centred weather stack, content cadences (sixth pass)
**Icons** were rebuilt after a visual bake-off (three style families rendered
dithered at panel scale, then two rounds of refinement — the sheets are
throwaway, the findings are these): the old set outlined every cloud puff
*separately*, so arcs crossed the inside of each cloud. Icons are now drawn
as a **union silhouette** (`_union`): the combined shape is painted black,
then the fill is painted on top, leaving one clean outline around the whole
form. Cloud uses four lobes — three read as a cloud, the small left one stops
it reading as a hill. The sun is a filled disc with *detached* rays; joined
thick rays read as a gear. `_weather_icon` now takes the Image as well as the
Draw, because mask-pasting needs the image itself.

**Layout:** the current temperature, °F line and sunrise/sunset row are each
centred on the three-day row below them (columns at 170/435/700 → centre
435), measured rather than hand-placed. Low is on the left and high on the
right in both units, everywhere.

**Content cadences:** `animal_period_days` / `question_period_days` (default
1, pane-editable) are independent of `refresh_minutes` — the panel still
redraws hourly so the temperature stays current, while the engraving and
question can hold for a week or a month. Implemented as a shared *content
slot* (`questions.slot_for` = ordinal // period + offset) that both widgets
key off; the animal cache is keyed by slot, so a long cadence also means one
Commons fetch per slot rather than per day.

## 20b. 228 animals, ocean-heavy, every entry API-verified (fifth pass)
The corpuses were never the limit — IZ alone is ~25k plates, and each animal
can resolve to a different plate in any of seven collections, so distinct
frames run to the thousands. The limit was our curated list, which needs a
trustworthy Latin term, native range and kind per entry to build the caption.
Grew it 85 → 228, with a large ocean section (paper nautilus, man o' war,
john dory, ratfish *Chimaera monstrosa*, sea slug, feather star, by-the-wind
sailor, moon jelly, lamprey, oyster, scallop, murex, triggerfish,
butterflyfish, pipefish, sea cucumber, crayfish, sea worm…).

**Every entry was validated against the live API**, not assumed: a sweep over
all 215 candidates found 9 that resolved nowhere. Three were 19th-century
naming drift (Harpia→**Harpyia**, Paradisaea→**Paradisea apoda**,
Thunnus→**Thynnus**); six had no plate under any name tried (whale, narwhal,
sperm whale, orca, sea otter, quetzal — the big cetaceans are simply absent
from these engraving corpuses) and were swapped for verified ocean species.
Re-verified after: 228/228 resolve. Re-run that sweep after editing `ANIMALS`.

**Bug fixed en route:** a genus-only title match let "Canis" label a jackal
as the wolf. A two-word Latin term now requires the full binomial in the
title, with no genus fallback — regression-tested both ways.

## 20. Minimal redesign (2026-09-17, third pass — per Matt's direction)
No header/location/date, no frames or dividers. The engraving becomes a soft
background: edge-faded (linear 100→0 over the outer 20% of every edge,
`_edge_fade_mask`) and anchored past the bottom-right corner so ~15% is cut
off. Caption (species · year · Iconographia Zoologica · University of
Amsterdam) top-right. The talking point runs along the bottom border, serif
italic, led by a red fleuron (❧ — drawn with DejaVu *Sans*; the Serif face
lacks the glyph). Display type is **Michroma**, the rage channel's caption
font, vendored from `rageagain_channel/fonts/` (OFL). Michroma's own `°`
renders as a clunky baseline "o", so `_deg()` draws degree marks as proper
rings. Sunrise/sunset use drawn sun-on-horizon glyphs. Text that can sit
over the artwork gets a white stroke halo so a dark plate never eats it.
