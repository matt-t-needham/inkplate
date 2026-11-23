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

**Anchored slots (2026-10-24):** `sleep_seconds` is the time to the next
wake slot, not a flat interval. Slots run every `refresh_minutes` from
`refresh_anchor` (local "HH:MM", default 06:00) and restart there each day
(`renderer.slot_window`), so a cadence that doesn't divide 24 h (150 min)
still refreshes at 06:00 — the day's last gap is just shorter (04:30 → 06:00).
A render is stale once it predates the current slot. The RTC drifts, so a wake
within `WAKE_GRACE_S` (10 min) before a slot is treated as that slot instead
of sleeping a few minutes and waking again.

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

## 29. Drag the print on the page itself (2026-11-22)
"The adjustment options need to be a grab and drag option from the panel
itself, not the sidepanel with delayed update." The pane's **Adjust** side
control is gone, and with it the render-on-release path: every drag used to
ask the server for a full dithered render before anything moved, and the
pane's control showed the art box alone, away from the page.

Now the pane's panel preview and the review page's page preview are what
you drag. One finger or the pointer moves the print; two fingers pinch it;
ctrl/cmd + scroll (a trackpad pinch) resizes it, as does a plain scroll on
the review page, which never scrolls itself; *Smaller* / *Larger*, the
arrow keys and + / - do the same for anyone who can't drag. While a finger
is down nothing is asked of the server: `/api/page-layers` has already
split the page in two (`screens.page_layers`), and the browser moves one
over the other.

- **The page, lifted off the art.** The screen is drawn twice by its own
  code with the art box filled white, then black, instead of the art
  (`_ART_FILL`, a context variable `_draw_art` honours). Where the two
  differ by d, what was drawn over the art has alpha 255 - d and the
  black-filled colour over that alpha (`_unblend`), so halos, the
  lightened patches under captions and text running over the print come
  out as a transparent overlay, and stay readable over the moving print.
- **The plate, unclipped.** `_draw_art` now returns how it fitted the plate
  (`ArtGeom`: sheet, subject box, automatic scale and origin), and
  `_art_layer` redraws it at the saved placement through the same
  `_placed_centre` `_place` uses, as far beyond the box as a drag can bring
  into view, its own edges faded as the mode fades them. The box's inner
  sides get the rest of the fades as CSS masks (`box_fades`).
- **What you see is what saves.** The browser clamps a draft exactly as
  the server does (`PLACEMENT_SHIFT`, `PLACEMENT_SCALE`, the subject's
  centre kept inside the box, relative to the automatic fit's centre the
  layers report). Paper, the plate clipped to the box and the page over it
  compose to the full render (pinned by a test), in the panel
  simulation's tones (`palette.simulate_tones`, an affine fit from the
  inks to `PANEL_SIM_PALETTE`), so the hand-over to the dithered picture
  is only the dither. The fades are the one approximation: the server
  fades the clipped art's edges, the live view the plate's and the box's.
- **Release saves** (`/api/placement`, unchanged; keys, buttons and the
  wheel after a 500 ms pause), the status reads Saving… / Saved / Save
  failed and why, then the real picture (the panel render on the pane,
  `/api/page-preview.png` on the review page) and fresh layers load in the
  background and replace the live view. Next/Previous still wait for the
  save.

Removed: `/api/animal-preview.png` and `screens.art_preview` (the art box
alone), the draft `x`/`y`/`scale` parameters of `/api/page-preview.png`
(it now draws the saved placement), and the Adjust button. The layers are
about a megabyte of WebP and take under a second, fetched once per print
and after each save.

## 28. The review page is a queue on the whole page (2026-11-22)
"The reviewing page needs to show it in the context of the page it will be
displayed in, including the blurb and the other content … it's just a queue
of the whole list. When moving to the next one, the current one is saved."
The card list (7/14/28-day picker, *Hide reviewed*, *Mark (not) reviewed*)
is gone. `/review` is now one print at a time over the next `REVIEW_DAYS`
(28) days, opening at `start`, the first print not reviewed yet (a day
still fetching counts as not reviewed; a day with no print is passed over).

The picture is the whole page: `/api/page-preview.png` draws the screen
(dashboard or daily, a switch on the page, defaulting to the panel's) at
the panel's current orientation through the screens' own code
(`screens.page_preview`, which hands the print to `_dashboard` / `_daily`
in place of today's), with today's cached weather and snow, or the daily
view's labelled example data, dithered and simulated like the wall. It
reports the art box as shares of the page (`X-Art-Box`), and
`placement.js` lays a box over the picture there: a drag inside it moves
the print, by shares of that box, exactly as the pane's Adjust does on the
art box alone (the pane is the same code with the box as the whole
picture). The placement is for the art layout the shown screen uses, so
the switch also switches which placement is edited. The page fits the
viewport and never scrolls; controls sit below it on a phone and beside it
above 900 px.

*Previous* and *Next* first let any pending save finish (a failed save
stops the move and says so), then mark the print reviewed. There is no
un-review: nothing used it once moving on is what marks a print. Unchanged:
autosave on release, Rotate, Veto (asks first), the background fetch with
a "Fetching…" wait for a day not fetched yet.

## 27. Full bleed on the dashboard (2026-11-22)
"Remove the fade of the image on the borders of the displays — it can be
full opacity up to the edge of the display, full bleed!" Every art layout
now follows the daily screen's rule (§26), in one mask (`_art_mask`): a side
of the print that lies on the panel's border is opaque right up to it; every
other side still fades, over the same fixed panel widths as before, because
there the print meets other content (the hero's weather band, the stack's
weather above it, the landscape weather and caption). To let prints reach
the border, the hero's art box now runs to the top, left and right borders
(it kept a 40 px `HERO_MARGIN` before; the caption still keeps it), and a
covering plate (a scene) in the landscape and stack layouts fills its box
exactly instead of being pushed `ART_BLEED` right, which left a faded strip
on the stack's left border. A contained figure still bleeds `ART_BLEED` off
the right as before. With the art opaque at the bottom border, the landscape
and stack snow report gets the hero's lightened patch under it
(`_fade_behind`) wherever it lands on the art.

Saved placements stay valid (they are relative to the art box), but a
hero placement made before this sits slightly differently, because its box
grew by the old margin on three sides.

## 26. The daily screen, on example data (2026-11-21)
A second real screen, `daily`, switched from the top of the pane
(Dashboard / Daily; the old Screen dropdown now only picks test patterns).
Portrait: this week's calendar (Monday to Sunday, local date per §11) and
one todo list on the left, the daily print on the right half. The print is
full bleed: every plate covers its box, a figure too, cropped around its
ink centroid. Unlike the dashboard (§24) this deliberately cuts ink: the
print reads as a picture filling its half, not a specimen on a sheet, and
Adjust can move or shrink it. Its only fade is on the box's inner left
edge, where it meets the calendar. Event titles may run on over the print
but stop short of its caption. Landscape keeps the print to the right
third with the list beside the calendar. The print has one art layout,
`daily`, for both orientations; `panel_layout` returns it whenever the
daily screen is up, so Adjust and the review page place the right thing,
previewed at the panel's current orientation.

On the panel tints would dither to noise and text must stay solid, so
everything is drawn in solid inks: **today** is a solid yellow bar down the
left of its day block; a **done** item has an outlined (unfilled) tag in
the owner's colour with a black letter, and its text and date are struck
through. Owner tags always carry the letter, never colour alone.

**The calendar and list are example data.** The shared calendar feed and
the planner service don't exist yet; `planner_stub.py` returns made-up
events and todos shaped like their future data, generated relative to the
current week, and the screen prints "Example data" along the bottom while
it is in use. Replace the stub with the real sources, keeping the shape.
The contract they must meet: events are per-day occurrences in local time;
the adapter expands multi-day and overnight events into one entry per day;
end times are dropped.

## 25. Per-image placement and a review page (2026-11-21)
The automatic crop gets most plates right but not all (a mount card, a
figure that wants to sit lower). **Placement** is a per-image correction,
**kept per layout**: config `image_placements[file][layout] = {x, y,
scale}`, where `layout` is the art box it was made in (`landscape`, `hero`,
`stack`: `screens.ART_LAYOUTS`). A nudge that suits the hero's centred box
is wrong for a bleed, so each art box keeps its own; the key is a plain
string the caller of `art_box`/`_draw_art` names, so a later screen (the
planner) adds its own. It is applied *after* the automatic trim/crop/fit,
inside the same art box (`screens._place`). Stored values are capped and the
drawn subject centre is kept inside the box, so a print can't leave its
area. No entry for the layout being drawn means the automatic fit, pixel for
pixel. Malformed entries are dropped on load (nothing was deployed with the
earlier one-placement-per-image shape, so there is no migration).

The pane's **Adjust** (replaced by dragging on the preview, §29) and the
**review page** (`/review`) share
`static/placement.js` and adjust the placement for the layout the panel is
using now (named under the control). Saving is automatic: a released drag
or slider saves, arrow-key nudges save after a 500 ms pause, *Reset to
automatic* saves at once; the status reads Saving… / Saved / the error. Its
picture is always a server render (`/api/animal-preview.png?layout=…`: the
dashboard's own `art_box` + `_draw_art`, dithered to the six inks), never a
CSS imitation — CSS only nudges the last render while a finger is down.

The review page lists the coming days' prints (28 since §28). They come from the same
slots (`slot_for`) and the same selection (`datasources._resolve`) as the
panel; upcoming slots are fetched into `cache/upcoming/<slot>.*`, never over
the panel's own `animal.*`. `/api/review` answers at once from that cache and
fetches missing slots in the background (a cold fetch of a fortnight can outlast
Cloudflare's 100 s timeout); the page re-polls while any are pending.

**When its day comes, the panel takes the upcoming print only if Matt
touched it** — marked it reviewed, or saved a placement or rotation for it —
and the slot still maps to that animal. So what was reviewed is what hangs
even if Commons changes in between, but an untouched upcoming print is resolved afresh, so selection-filter fixes made meanwhile still
apply to it. `reviewed_images` in config records what has been worked
through. Old placements and reviewed marks are not pruned: one short entry
per image, a few hundred a year at most.

## 24. Bad plates and a beheaded lark: tighter validation, no-cut zoom (2026-10-25)

Matt reported a run of bad images and a poorly cut top. Nothing recorded which
file was shown on which day, so the fortnight 10-13…10-25 was replayed against
live Commons (deterministic slots; search results drift, so this is a close
reconstruction, not a record). Six of thirteen were wrong:

| Day | Shown as | Actually | Why it passed |
|---|---|---|---|
| 10-17 | colugo | IZ sheet, half of it a skeleton sketch | no hint anywhere in the metadata — veto it |
| 10-18 | hyrax | a skull on a plinth (Buffon) | anatomy only in description/categories |
| 10-19 | turkey | "The Turkey Buzzard" (Catesby) — a vulture | common name matched as a whole word |
| 10-20 | pheasant | Audubon's ruffed grouse, "Vulgo Pheasant" | same, and Audubon drew no pheasants |
| 10-23 | dove | a 2023 wildlife photo | full-text "John Gould dove"; modern-photo fallback took it |
| 10-25 am | nightingale | a photo of Florence Nightingale (Hering, 1858) | full-text "John Gould nightingale" (cycled away at 09:56) |
| 10-25 | lark | Catesby's "large Lark" — the eastern meadowlark | *Alauda magna* in the title; Catesby drew no larks |

The vetoed `Quail 4.jpg` (10-22) was the Aerosport Quail, a light aircraft.

**Validation added** (`_try_collection` / `get_animal`):
- **Corpus evidence.** Each collection has an `evidence` regex that the
  title, description, categories, artist or credit must match. Kills the
  aircraft and the dove photo; the modern-photo fallback still works for real
  photographs *of a plate*, which name the book.
- **Anatomy in the description and categories**, not just the title, plus
  "skull".
- **Namesakes by taxon.** A file matched only by the common name is rejected
  when its Commons `<taxon> (illustrations)` categories name a different
  genus. A Latin match is exempt: period genera often differ from Commons'
  modern ones (our colugo is *Galeopithecus*, Commons files it under
  *Galeopterus*), so the check would throw out right answers.
- **New World corpora.** Catesby and Audubon are `new_world`; an animal whose
  native range names only Old World places (`_could_be_new_world`) is never
  looked for there. Vague ranges ("worldwide", "coastal seas") stay eligible.
- **Known namesakes** (`NAMESAKES`, common name → title regex) for the
  traps no rule can tell apart: "Squilla mantis" (mantis shrimp) has the same
  shape as "Phascolomys wombat" (the wombat, period genus), so a general
  "common name as another genus's epithet" rule was tried and dropped — it
  lost the wombat and "Alpine marmot". Listed: mantis, lion (Buffon's
  "Chien lion" lap dog), buzzard (Catesby's *Buteo … Gallinae Pavonis*, the
  turkey vulture), magpie (Audubon's "Columbia Magpie or Jay", *Pica
  bullockii* to him, the Mexican black-throated magpie-jay — shown
  2026-10-26).
- **NYPL book shots ranked last.** NYPL's Audubon files are photos of the open
  book with a Kodak colour bar; the subject crop locked onto the bar and the
  page edges (2026-10-26). Files titled "(NYPL …" or credited "Scan by …" sort
  after real plate scans, like modern photos, and "Scan by NYPL" is never
  printed as the artist — the corpus's artist is credited instead.
- **Faint or blank downloads** (`_too_faint`): ink coverage of the subject box,
  measured with the renderer's own `_prep_plate`/`_ink_mask`. Five blank scans
  and pencil sketches in the sweep measured 0.2–3.3%, the faintest real plate
  20%, so `FAINT_INK` = 5%. A faint pick falls to the next file in the pool
  (up to `FAINT_RETRIES` downloads), then to the next collection.
- **Each pick is logged** (`animal for <day>: <common> via <coll>: <file>`)
  so the next "that was a bad one" can be traced without a replay.
- **Dutch anatomy tokens** IZ uses: kaak (jaw), snavel (beak), tanden,
  hoorns, baarmoeder, geslachtsorganen, maag, darmen, hersenen.

**Sweep** (old rules vs new over the next 227 days = every animal once,
Commons responses shared): 64 picks change; all 227 still resolve. Among the
old picks the new rules remove: a photo of financier Jay Gould (jay — the
first Gould evidence regex, a bare `\bGould\b`, let him in; it now needs
"John Gould", "(Gould)", "GouldBirds…" or Richter), a painting "Tom Roberts at
Curlew Camp", "Longhorn Spanish cattle" (bustard), a hornbill beak
(rhinoceros), Catesby's Carolina parakeet as the African grey, Audubon's
meadowlark as lark and as starling, a "Nightingale letter", wildlife photos
(penguin, oriole, lynx), a buffalo's genitals and the lap dog. Three ANIMALS
terms had only ever matched anatomy or namesakes and were changed to the name
the corpus files them under: lion `Panthera leo` → **Felis leo**, buffalo
`Bubalus` → **Bos bubalis**, alligator `Alligator` → **Crocodilus lucius**.

Not caught: composite sheets like the colugo's (title, description and
categories all just name the species), and IZ pages that are mostly a
newspaper clipping (the sweep's aye-aye and pangolin). The per-image veto is
the answer.

**Crop.** `ART_ZOOM` scaled a figure 15% past its fit and cropped the
overflow around the ink centroid, on the claim that the overflow was paper.
It wasn't: the subject box keeps only 3% padding, and on the lark the rock
and plant outweigh the neck, so the whole overflow came off the top — with
the head. The zoom is now capped so the ink span (`_ink_span`) plus the hero
fade still fits, and the crop window is clamped so it never cuts ink. Most
plates fill their subject box, so in practice the lean-in is now small.
Tried and dropped: growing the subject box outward to follow thin parts (a
beak tip just under the run threshold) — it followed IZ mount cards and
pencil annotations and shrank those plates badly.

## 23. Only repeated fetch failures are reported (2026-10-16)

Open-Meteo 503'd once on 2026-10-15 and was fine on the next fetch, but each
blip logged a WARNING plus an error event and showed up as six error groups in
the nightly log review. `_cached_fetch` now keeps the run of consecutive
failures in the cache entry (so it survives restarts). A failure that still has
a cached copy to serve is logged at INFO, worded so the log compiler doesn't
flag it. The WARNING and pane event come at `REPEATED_FAILURES` (3) in a row, or
straight away when nothing is cached. A recovery after a reported run is logged
as an event.

## 22. Quote removed (2026-10-16)

The quote had been off by default since §21; Matt asked for it gone. `quotes.py`,
its tests, the `show_quote`/`vetoed_quotes` config keys (dropped on load from
an older `config.json`), the pane's quote card, *Next quote* and *Veto quote*,
and the `quote` targets of `/api/cycle` and `/api/veto` (now 400) are all
removed. The landscape art box keeps the quote-off geometry. §20g and §21
describe the feature as it was.

## 21. Rotation on the server, portrait layouts, quote off (2026-10-13)
**Rotation is a config value, not a firmware one.** The panel can hang
vertically (`rotation` = its clockwise turn from landscape: 0/90/180/270).
The screen is drawn upright at the swapped size (1200×1600), the three pane
previews stay upright, and only the device frame (`render/frame.png`) is
turned back to native 1600×1200 before it is saved. The device protocol is
untouched and the firmware never learns about orientation — which matters
because the sketch is unverified on hardware and every contract change is a
reflash. The frame is what gets hashed, so turning the panel bumps the
version and forces the redraw it needs. PIL's `rotate()` is anticlockwise;
turning the upright image by the panel's clockwise angle is the inverse the
frame needs (a `(0,0)` mark lands bottom-left in the frame for 90°, which is
where the native top-left corner goes when the panel turns clockwise).

**Two portrait arrangements, picked in the pane** (`portrait_layout`), so
they can be compared on the wall rather than argued about from mockups:
- *hero* (default — Matt picked it, and portrait is now the primary hanging,
  so `rotation` defaults to 90): the bottom third is the weather band
  (weather left; now-playing and the snow report right), the top two thirds
  are the art section. The plate is centred there on its **ink centroid**,
  not its bounding box (a heron is a big body under a thin neck; the eye
  centres the body), clamped inside the section. The long edge fades go: a
  36 px softening only. The caption sits top-right, on the art. Below, a
  **symmetric band** (the landscape weather block, left-aligned, read as
  cramped under a centred image): the temperature row centred; under it two
  grouped blocks hugging ±360 px of centre — the next two days on the left,
  sunrise over sunset on the right. The snow report is one centred row along
  the bottom border, its label with clear air above the glyphs. With snow
  off the band drops to the border and the art section grows by ~150 px.
  Anything laid over the art — caption, weather — is measured first and the
  art lightened under it with a feathered white patch (`_fade_behind`): fade
  the image for the text, not the whole edge.

**Now-playing is gone** (fetcher, config flag, fast render TTL, pane
checkbox). Matt will never use it on the wall; §18 stays as history.
- *stack*: the landscape arrangement folded — weather top-left, caption
  top-right, engraving across the lower two thirds, snow bottom-left over the
  art's faded edge. Kept as the alternative.

One portrait-specific rule: in a 1200 px-wide frame the weather's first row
(glyph · 21° · HIGH 28°) reaches x≈920, so the caption cannot share that row
as it does in landscape. It tucks under the HIGH temperature instead
(`PORTRAIT_CAPTION_TOP`). The first mockups had it colliding with the HIGH.

**Quote off by default, not removed** (`show_quote`). Matt wanted the clutter
gone; the fetcher, the no-repeat history, vetoes and the pane card stay so it
can come back with one checkbox. With it off the art box in landscape grows
from (560,150)–(1600,1150) to (440,140)–(1600,1200): a tall plate gains ~6%
height, a wide one ~11% width, and the engraving now runs to the bottom
border. The quote is not fetched while hidden, so Wikiquote sees no traffic.

Layout previews live in `docs/layouts/` — rendered with *synthetic* plates
(a hatched heron and crocodile drawn in code) because Commons was blocked
from the sandbox they were made in; they show placement, not real art.

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

## 20g. Questions retired for Wikiquote's quote of the day (tenth pass)
The curated questions never read well, even after the 20f rewrite, so the
talking point is now a real quote: Wikiquote's Quote of the Day, fetched
daily. The bank, its editor, question vetoes and cadence are gone.

**Attribution and date are the point.** A QOTD page gives only the text and
an author link, so each candidate is looked up on the author's page: it must
appear outside any Disputed / Misattributed / Attributed / "about" section,
and the citation under it (or the section heading) must yield a year. Work
title comes from the citation's italics or the section heading. Fail any of
that and the candidate is skipped — the panel never shows an unplaced quote.

**Fit before fetch.** `screens.quote_fits` wraps the text in the real font at
panel size; over three lines and it's skipped, never truncated. Measured at
1600×1200 always, so a small test render can't change the day's pick.

**No repeats.** Replacements come from seeded random archive dates strictly
before 2026-10-07 (the day this shipped), so a fallback can't be a quote the
live feed will serve later; a history of shown quotes catches Wikiquote's own
re-runs. A day that finds nothing backs off 30 min between sweeps.

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

## 20f. Lean into the plate; turn sideways scans; baboon → Papio (2026-10-13)
Three things Matt saw on the wall. **Whitespace:** the subject crop padded
6% and a contained figure then floated in whatever the box didn't share its
aspect with. The pad is 3% now, and `_draw_art` scales a figure past its fit
by `ART_ZOOM` (1.15) and crops the overflow around the ink centroid — the
cut is paper margin, and the fixed-width fades swallow the edge. Scenes
still cover. **Sideways plates** (a landscape figure bound into a portrait
book — the gorilla on a newspaper clipping): no automatic fix. The turn's
*direction* is the hard part and a wrong guess hangs the beast upside down,
which is worse than sideways; the pane gets a **Rotate ↻** button instead,
a quarter clockwise per click, stored per Commons file in config
`image_rotations` (like `vetoed_images`), applied before the crop so the
fades and the centroid see the plate upright. Four clicks bring it round and
the entry drops out of config. **"Baboon" that wasn't:** the Latin term was
the period *Cynocephalus*, which modern taxonomy gives to the colugo
(*Cynocephalus volans*), so a titled, perfectly genuine plate of a flying
lemur captioned itself a baboon — the title filter did its job, the term
lied. Now *Papio*. The remaining period names were read for the same drift;
*Harpyia* (harpy eagle) is also a moth genus and is the one to watch at the
next sweep. Could not run the live resolution sweep from this session
(Commons unreachable) — re-run it before trusting the baboon pool.

## 20e. Genus must lead the binomial; vetoes are per image (2026-10-09)
"Butterfly" (*Papilio*) showed a fish twice: the title match was a substring
test, so a genus-only term matched any *epithet* — *Pteraclis papilio* (a
fish), *Periophthalmus … ε Papilio* (a mudskipper), *Strombus papilio* (a
snail) — and "butterfly" matched butterflyfish. 216 of the 228 entries are
genus-only; a live sweep found impostor plates in 40 animals' pools (peacock
→ wrasse *Crenilabrus pavo*, turkey → snake *Acontias meleagris*, hawk moth
→ *Mandrillus sphinx*, porcupine → sea urchins…).

Now a one-word term must lead the file name, or appear capitalised with a
lowercase epithet after it; the common name must be a whole word. The
substring genus fallback is gone. Some genuine plates filed under old
binomials (*Canis vulpes*, *Camelopardalis giraffa*, *Rana bufo*) drop out
of their pools; every animal still resolves.

Re-swept all 228: seven no longer resolved. Moose → **Cervus alces**,
chamois → **Antilope rupicapra**, gazelle → **Antilope gazella** (genuine
plates under old binomials); shark → **Carcharias**, tropicbird →
**Phaeton** (period spelling), scorpion → **Buthus** — these had only ever
matched impostors (a flatworm, a hummingbird, a crab). **Dolphin removed**:
its only matches were a fish louse and a microbe, and no corpus has a titled
*Delphinus* plate. 227 entries, all verified.

The pane's veto now bans the **image** (its Commons file title, kept in
`vetoed_images`), not the animal — the animal is redrawn from another plate.
Vetoes are permanent; the restore chips and `/api/unveto` are gone.

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
