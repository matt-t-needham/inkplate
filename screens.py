"""Screen registry — each screen is a function that draws an RGB frame.

Extension point: write a function that takes (width, height) and returns a PIL
RGB image, add it to SCREENS, and it appears in the control pane's picker.
Screens are size-agnostic: the renderer asks for 1600x1200 when the panel
hangs landscape and 1200x1600 when it is rotated to portrait (it turns the
finished frame back to the panel's native orientation itself).

Screens:
- dashboard       — the real thing: weather, daily 1800s animal engraving,
                    seasonal snow report; its own
                    portrait layout when taller than wide
- daily           — this week's calendar and the todo list beside the daily
                    animal (todos from the planner; example events)
- planner         — the daily screen or the bingo card, as the planner's
                    export asks
- bingo           — the planner's chosen 4x4 bingo card
- placeholder     — proves the whole pipeline end to end
- palette_test    — six solid color bands; verifies color mapping on the panel
- dither_test     — gradients; judges dither quality
- alignment       — border, crosshairs, corner labels; catches offset/rotation
"""

import contextvars
import logging
import math
from datetime import date, datetime, timedelta
from typing import NamedTuple

from PIL import Image, ImageDraw, ImageFont

import config
import datasources
import planner_source
from palette import SPECTRA_COLORS

log = logging.getLogger("inkplate.screens")

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
YELLOW = (255, 255, 0)
RED = (255, 0, 0)
BLUE = (0, 0, 255)
GREEN = (0, 255, 0)


def _font(size: int, bold: bool = False,
          style: str = "sans") -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """style: 'sans' (DejaVu), 'display' (Michroma — the rage channel font),
    'serif_italic' (DejaVu Serif Italic). Falls through path lists in order."""
    paths = {
        "display": config.FONT_DISPLAY_PATHS + config.FONT_BOLD_PATHS,
        "serif_italic": config.FONT_SERIF_ITALIC_PATHS,
        "sans": config.FONT_BOLD_PATHS if bold else config.FONT_PATHS,
    }[style]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _centered_text(draw: ImageDraw.ImageDraw, xy, text, font, fill):
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    w, h = right - left, bottom - top
    draw.text((xy[0] - w / 2 - left, xy[1] - h / 2 - top), text, font=font, fill=fill)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    """Greedy word wrap by measured pixel width."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


# ── weather icons ─────────────────────────────────────────────────────────────
# Weather Icons by Erik Flowers (SIL OFL 1.1, vendored in fonts/ — see
# fonts/README.txt). Monochrome line-art glyphs: they are drawn filled, never
# stroked-and-filled, because stroking line art doubles every outline. Colour
# carries meaning the same way it does everywhere else on the panel — yellow
# for sun/lightning, blue for anything falling out of the sky, black for
# cloud and fog. Codepoints are Private Use Area, so they are written as
# hex escapes; pasting the literal characters does not survive every pipe.
WI_GLYPHS = {
    "sun": 0xf00d, "partly": 0xf002, "cloud": 0xf013, "fog": 0xf014,
    "drizzle": 0xf01c, "rain": 0xf019, "showers": 0xf01a, "sleet": 0xf0b5,
    "snow": 0xf01b, "storm": 0xf01e, "hail": 0xf015,
}
# Icons are drawn in one ink on purpose. Spectra has no greys — "grayscale"
# on this panel means black — and coloured icons competed with the engraving
# behind them for attention. Black also keeps every glyph at equal weight.
ICON_INK = BLACK

WI_SUNRISE, WI_SUNSET = 0xf051, 0xf052

_wi_cache: dict[int, ImageFont.FreeTypeFont] = {}


def _wi_font(size: int):
    if size not in _wi_cache:
        for p in config.FONT_WEATHER_PATHS:
            try:
                _wi_cache[size] = ImageFont.truetype(p, size)
                break
            except OSError:
                continue
        else:
            _wi_cache[size] = ImageFont.load_default()
    return _wi_cache[size]


def _glyph(d: ImageDraw.ImageDraw, cx: int, cy: int, size: int, codepoint: int,
           fill, stroke: int = 0, stroke_fill=BLACK):
    """Draw one Weather Icons glyph centred on (cx, cy). Returns its width."""
    ch = chr(codepoint)
    f = _wi_font(size)
    l, t, r, b = d.textbbox((0, 0), ch, font=f, stroke_width=stroke)
    d.text((cx - (r - l) / 2 - l, cy - (b - t) / 2 - t), ch, font=f, fill=fill,
           stroke_width=stroke, stroke_fill=stroke_fill)
    return r - l


def _glyph_width(d: ImageDraw.ImageDraw, codepoint: int, size: int, stroke: int = 0) -> int:
    l, _, r, _ = d.textbbox((0, 0), chr(codepoint), font=_wi_font(size),
                            stroke_width=stroke)
    return r - l


def _icon_size(r: int) -> int:
    """Glyph point size for a nominal icon radius (matches the old drawn set)."""
    return int(r * 2.7)


def _weather_icon(d: ImageDraw.ImageDraw, cx, cy, r: int, kind: str) -> int:
    """kind ∈ WI_GLYPHS (datasources.icon_kind). Returns the drawn width."""
    cp = WI_GLYPHS.get(kind, WI_GLYPHS["cloud"])
    return _glyph(d, int(cx), int(cy), _icon_size(r), cp, ICON_INK)


def _weather_icon_width(d: ImageDraw.ImageDraw, r: int, kind: str) -> int:
    """Measured width, for centring a row before anything is drawn."""
    cp = WI_GLYPHS.get(kind, WI_GLYPHS["cloud"])
    return _glyph_width(d, cp, _icon_size(r))


# ── plate preparation ─────────────────────────────────────────────────────────
# These are full-sheet scans of bound books: a figure somewhere on the page,
# surrounded by margins, printed captions, handwritten annotations, and often
# near-black scan edges. Standardising them is the same problem a CMS solves
# with trim → smart-crop → fixed aspect box, so it is solved the same way:
#
#   1. _trim_borders  — drop near-solid dark scan edges (they are denser than
#                       any figure, so a content search locks onto them).
#   2. _subject_box   — projection-profile analysis: the figure is the densest
#                       contiguous band of ink on each axis, while captions and
#                       marginalia are thin sparse ones. An absolute bounding
#                       box cannot tell those apart — one stray mark in a
#                       corner stretches it to the whole sheet, which is what
#                       used to make the crop silently give up.
#   3. white point    — paper tone becomes pure white so the dither doesn't
#                       speckle the background with colour noise.
#
# Scenes (a landscape engraving) are distinguished from figures (an animal on
# blank paper) by ink coverage, because the two want opposite fitting rules.

# Measured across the corpuses: single figures top out near 39% ink (a heron
# filling its sheet), true full-bleed scenes run 60%+ (a forest engraving).
SCENE_INK_COVERAGE = 0.50

# How much of the artwork runs off the right border, and how wide the fades
# are in panel pixels (constant, so every plate dissolves identically).
ART_BLEED = 0.075
ART_FADE_LEFT = 300
ART_FADE_EDGE = 120
# A contained figure is scaled past its fit by up to this much and the
# overflow cropped: the subject box keeps a margin of paper and a fit-to-box
# plate still floats in white, so lean in — but only as far as that paper
# goes. The crop never cuts ink: a figure whose ink fills its box is drawn
# at fit (cutting around the ink centroid took the lark's head off, because
# its rock and plant outweigh its neck and beak).
ART_ZOOM = 1.15


def _paper_tone(g: Image.Image, pct: float = 0.75) -> int:
    """Estimate the paper's grey level as a high percentile of the histogram.
    Not the median: on a densely inked plate the median pixel is ink, which
    inverts the whole estimate. The 75th percentile keeps paper as the
    reference until ink covers three quarters of the sheet."""
    hist = g.histogram()
    total, acc = sum(hist), 0
    for v, c in enumerate(hist):
        acc += c
        if acc >= total * pct:
            return v
    return 255


def _ink_mask(img: Image.Image, small: int = 320):
    """Downscaled black-ink mask, thresholded relative to the paper tone."""
    from PIL import ImageFilter

    g = img.convert("L")
    g.thumbnail((small, small))
    g = g.filter(ImageFilter.MedianFilter(3))
    paper = _paper_tone(g)
    return g.point(lambda v: 255 if v < max(60, int(paper * 0.82)) else 0), g.size


def _profiles(mask: Image.Image, size):
    """Per-column and per-row ink counts."""
    w, h = size
    cols, rows = [0] * w, [0] * h
    for i, v in enumerate(mask.getdata()):
        if v:
            cols[i % w] += 1
            rows[i // w] += 1
    return cols, rows


def _dominant_run(prof, k: float = 0.08, gap_frac: float = 0.06):
    """The band carrying the most ink, tolerating small gaps within it."""
    if not prof or max(prof) == 0:
        return 0, len(prof) - 1
    thr = max(prof) * k
    gap = max(2, int(len(prof) * gap_frac))
    runs, start, miss = [], None, 0
    for i, v in enumerate(prof):
        if v >= thr:
            if start is None:
                start = i
            miss = 0
        elif start is not None:
            miss += 1
            if miss > gap:
                runs.append((start, i - miss))
                start = None
    if start is not None:
        runs.append((start, len(prof) - 1))
    if not runs:
        return 0, len(prof) - 1
    return max(runs, key=lambda r: sum(prof[r[0]:r[1] + 1]))


def _trim_borders(img: Image.Image, dark: float = 0.62, max_frac: float = 0.18):
    """Strip near-solid dark edges left by scanning a bound book."""
    mask, (sw, sh) = _ink_mask(img)
    cols, rows = _profiles(mask, (sw, sh))

    def edge(prof, n, other):
        lim, lo, hi = int(n * max_frac), 0, n - 1
        while lo < lim and prof[lo] >= other * dark:
            lo += 1
        while hi > n - 1 - lim and prof[hi] >= other * dark:
            hi -= 1
        return lo, hi

    x0, x1 = edge(cols, sw, sh)
    y0, y1 = edge(rows, sh, sw)
    W, H = img.size
    sx, sy = W / sw, H / sh
    return img.crop((int(x0 * sx), int(y0 * sy), int((x1 + 1) * sx), int((y1 + 1) * sy)))


def _subject_box(img: Image.Image, pad: float = 0.03):
    mask, (sw, sh) = _ink_mask(img)
    cols, rows = _profiles(mask, (sw, sh))
    x0, x1 = _dominant_run(cols)
    y0, y1 = _dominant_run(rows)
    W, H = img.size
    sx, sy = W / sw, H / sh
    px, py = (x1 - x0) * pad * sx, (y1 - y0) * pad * sy
    return (max(0, int(x0 * sx - px)), max(0, int(y0 * sy - py)),
            min(W, int((x1 + 1) * sx + px)), min(H, int((y1 + 1) * sy + py)))


def _prep_plate(art: Image.Image):
    """Trim and clean the whole sheet, and find the subject on it. Returns
    (sheet, subject_box, is_scene): the sheet keeps the paper around the
    subject, so a layout with room to spare can show some of it."""
    art = _trim_borders(art)

    mask, size = _ink_mask(art)
    coverage = sum(1 for v in mask.getdata() if v) / (size[0] * size[1])
    is_scene = coverage > SCENE_INK_COVERAGE

    box = _subject_box(art)
    W, H = art.size
    if (box[2] - box[0]) * (box[3] - box[1]) < 0.02 * W * H:
        box = (0, 0, W, H)   # guard only against a degenerate sliver

    # White point from the subject, not the sheet: its margins are paper.
    wp = max(180, min(240, _paper_tone(art.crop(box).convert("L")) - 5))
    return art.point(lambda v: 255 if v >= wp else int(v * 255 / wp)), box, is_scene


def _prep_engraving(art: Image.Image) -> tuple[Image.Image, bool]:
    """Trim, crop to the subject, clean the paper. Returns (image, is_scene)."""
    sheet, box, is_scene = _prep_plate(art)
    return sheet.crop(box), is_scene


def _c_to_f(c: float) -> int:
    return round(c * 9 / 5 + 32)


def _hhmm(iso: str) -> str:
    try:
        return iso.split("T")[1][:5]
    except (IndexError, AttributeError):
        return "--:--"




def _deg(d: ImageDraw.ImageDraw, x: int, y: int, text: str, size: int, fill,
         suffix: str = "", halo: dict | None = None, measure: bool = False) -> int:
    """Draw `text` in Michroma followed by a drawn degree ring (Michroma's own
    ° glyph renders as a clunky baseline 'o'), then an optional suffix ('F').
    Returns the total width; measure=True computes it without drawing."""
    f = _font(size, style="display")
    r = max(2, int(size * 0.11))
    gap = max(2, size // 24)
    w_text = d.textlength(text, font=f)
    w = w_text + gap + 2 * r + (gap + d.textlength(suffix, font=f) if suffix else 0)
    if measure:
        return int(w)
    halo = halo or {}
    d.text((x, y), text, font=f, fill=fill, **halo)
    cx = x + w_text + gap + r
    cy = y + int(size * 0.30)
    ow = max(2, size // 20)
    if halo:
        pad = halo.get("stroke_width", 2)
        d.ellipse([cx - r - pad, cy - r - pad, cx + r + pad, cy + r + pad],
                  outline=halo.get("stroke_fill", WHITE), width=ow + 2 * pad)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=fill, width=ow)
    if suffix:
        d.text((cx + r + gap, y), suffix, font=f, fill=fill, **halo)
    return int(w)


def _sun_horizon(d: ImageDraw.ImageDraw, x: int, y: int, size: int, rising: bool) -> int:
    """Sunrise/sunset glyph centred on (x, y). Needs to be reasonably large:
    the two differ only by the chevron under the sun, which vanishes below
    ~70px. Returns the drawn width."""
    return _glyph(d, x, y, size, WI_SUNRISE if rising else WI_SUNSET, ICON_INK)


# ── dashboard ─────────────────────────────────────────────────────────────────
# One set of widgets, two orientations. Every widget takes the draw context
# plus the X/Y scalers, so the same code lays out a 1600x1200 landscape frame
# and a 1200x1600 portrait one (the panel hung on its side — renderer turns
# the finished frame back to the panel's native orientation, the device never
# knows). Portrait has two arrangements, picked in the pane:
#
#   hero  — (default) the engraving sits centred in the top two thirds with
#           its caption overlaid top-right; below it a symmetric weather band
#           (temperature centred; forecast block left, rise/set block right)
#           and, along the bottom border, a centred snow row. With snow off
#           the band drops to the border and the art grows into the room.
#   stack — weather top-left, caption top-right, the engraving fills the lower
#           two thirds; snow report bottom-left over the art's faded edge.

LANDSCAPE = (1600, 1200)   # layout coordinates; renders scale from these
PORTRAIT = (1200, 1600)

PORTRAIT_LAYOUTS = ("hero", "stack")
DEFAULT_PORTRAIT_LAYOUT = "hero"
# The art boxes a placement can be saved for (art_box). Each is a plain
# string named by the caller of art_box/_draw_art; a new screen with its own
# art box adds its key here.
LANDSCAPE_LAYOUT = "landscape"
# The daily screen's art, in landscape only: in portrait the print hangs
# whole as a polaroid, with no art box to place it in (DECISIONS §30).
DAILY_LAYOUT = "daily"
ART_LAYOUTS = (LANDSCAPE_LAYOUT, *PORTRAIT_LAYOUTS, DAILY_LAYOUT)
ART_LAYOUT_NAMES = {"landscape": "landscape layout", "hero": "portrait hero layout",
                    "stack": "portrait stack layout", "daily": "daily view"}

WEATHER_BLOCK_H = 440      # top of the glyph row to the bottom of the °F line
# Portrait: the weather's first row (glyph · 21° · HIGH 28°) reaches x≈920 of
# 1200, so the caption can't share that row as it does in landscape — it
# tucks under the HIGH temperature instead, right-aligned.
PORTRAIT_CAPTION_TOP = 196
# Hero: a centred composition. The weather band (_draw_weather_centred,
# HERO_WEATHER_H tall) sits on the snow row (_snow_foot, HERO_SNOW_H tall)
# or, with snow off, on the bottom margin; everything above it, out to the
# top, left and right borders, is the art section. Art that reaches a
# border is opaque up to it (full bleed); every other edge gets a soft
# HERO_FADE — enough to hide the crop line, not a dissolve. The subject
# crop is tight (a beak or a tail tip can sit on it), so wherever the box
# has room the plate is drawn HERO_CONTEXT further out and the fade lands
# on that paper margin; only where the box itself stops it — the weather
# band — does the fade fall on the drawing. The caption keeps HERO_MARGIN
# from the top border.
HERO_MARGIN = 40
HERO_FADE = 36
HERO_CONTEXT = 80
HERO_WEATHER_H = 340       # centred temperature row + forecast / rise-set row
HERO_SNOW_H = 118          # location label over one row of figures
HERO_BAND_HALF = 360       # the forecast and rise/set blocks hug ±this of centre
# Text that sits on the art (caption, weather) has the art lightened under
# it — a feathered white patch — so it reads without a heavy halo.
CAPTION_FADE_STRENGTH = 0.82
CAPTION_FADE_PAD = 22
CAPTION_FADE_FEATHER = 28


def with_image_settings(animal: dict | None, cfg: dict) -> dict | None:
    """The animal dict plus its per-image settings from config, keyed by the
    Commons file: "rotation" (clockwise degrees, for plates scanned
    sideways) and "placements" ({art layout: the pane's move/scale}, see
    normalize_placement; a layout with no entry gets the automatic fit).
    The cached dict is left untouched."""
    if not animal:
        return animal
    f = animal.get("file")
    return {**animal,
            "rotation": int((cfg.get("image_rotations") or {}).get(f, 0)) % 360,
            "placements": dict((cfg.get("image_placements") or {}).get(f) or {})}


def panel_layout(cfg: dict, size: tuple[int, int]) -> str | None:
    """The art layout the panel draws at this frame size: the daily screen's
    own, else the dashboard's — landscape, or the configured portrait
    layout. None for the portrait daily screen, whose print is shown whole
    and can't be placed, and for bingo (no print); "planner" is resolved
    first."""
    width, height = size
    screen = drawn_screen(cfg.get("screen"))
    if screen == "daily":
        return DAILY_LAYOUT if width >= height else None
    if screen == "bingo":
        return None
    if height > width:
        lay = cfg.get("portrait_layout")
        return lay if lay in PORTRAIT_LAYOUTS else DEFAULT_PORTRAIT_LAYOUT
    return LANDSCAPE_LAYOUT


# Per-image placement: dragging the print on the pane or the review page
# moves and scales an image inside one layout's art box, after the automatic
# trim/crop/fit has placed it. Placements are kept per layout (config
# image_placements[file][layout]), because a nudge that suits the hero's
# centred box is wrong for a bleed. x and
# y shift the subject's centre by that share of the box's width and height;
# scale multiplies the automatic scale. Stored values are kept inside these
# limits, and _draw_art also keeps the subject's centre inside the box, so an
# image can never be pushed or shrunk out of its own area.
PLACEMENT_SHIFT = 1.0
PLACEMENT_SCALE = (0.5, 2.5)
DEFAULT_PLACEMENT = {"x": 0.0, "y": 0.0, "scale": 1.0}


def normalize_placement(p) -> dict | None:
    """A placement mapping clamped to the limits, or None when it is the
    automatic one (nothing to store). Raises ValueError on junk."""
    if p is None:
        return None
    if not isinstance(p, dict):
        raise ValueError("placement must be a mapping of x, y and scale")
    try:
        x = float(p.get("x", 0.0))
        y = float(p.get("y", 0.0))
        scale = float(p.get("scale", 1.0))
    except (TypeError, ValueError):
        raise ValueError("placement x, y and scale must be numbers") from None
    if not all(math.isfinite(v) for v in (x, y, scale)):
        raise ValueError("placement x, y and scale must be finite")
    out = {"x": round(max(-PLACEMENT_SHIFT, min(PLACEMENT_SHIFT, x)), 4),
           "y": round(max(-PLACEMENT_SHIFT, min(PLACEMENT_SHIFT, y)), 4),
           "scale": round(max(PLACEMENT_SCALE[0], min(PLACEMENT_SCALE[1], scale)), 4)}
    return None if out == DEFAULT_PLACEMENT else out


def _animal(cfg: dict) -> dict | None:
    """Today's animal with its per-image settings, or None."""
    return with_image_settings(
        datasources.get_animal(offset=cfg["animal_offset"],
                               period_days=cfg["animal_period_days"],
                               vetoed=cfg["vetoed_images"],
                               keep={*cfg["reviewed_images"], *cfg["image_placements"], *cfg["image_rotations"]}),
        cfg)


def _dashboard_data(cfg: dict, animal: dict | None) -> dict:
    """Every source the dashboard draws from, each independently optional;
    the print is the caller's (today's, or one the review page previews)."""
    return {
        "weather": datasources.get_weather(cfg["latitude"], cfg["longitude"]),
        "animal": animal,
        "snow": (datasources.get_snow(cfg["snow_latitude"], cfg["snow_longitude"])
                 if cfg["show_snow"] else None),
    }


def dashboard(width: int, height: int) -> Image.Image:
    """Minimal dashboard. The daily engraving is a soft, edge-faded background
    bleeding off the right border; weather sits top-left; source caption
    top-right; the snow report (seasonal) along the bottom. Display type is
    Michroma (the rage channel font), the caption is serif italic. Every widget degrades independently — a dead
    API never blanks the panel. Taller than wide means the panel is hung
    vertically and the portrait layout is used."""
    # Lazy import: state imports screens (for DEFAULT_SCREEN), so importing
    # state at module top would be circular. At call time it's fully loaded.
    import state
    cfg = state.load_config()
    return _dashboard(width, height, cfg, _dashboard_data(cfg, _animal(cfg)))[0]


def _dashboard(width: int, height: int, cfg: dict, data: dict):
    """The dashboard drawn from `data`: (image, the Art drawn or None)."""
    if height > width:
        return _dashboard_portrait(width, height, cfg, data,
                                   layout=cfg.get("portrait_layout"))
    return _dashboard_landscape(width, height, cfg, data)


# The screens that hang a print, and so can preview one on the review page.
PRINT_SCREENS = ("dashboard", "daily")


def _page(screen: str, animal: dict | None, cfg: dict, size: tuple[int, int]):
    """The whole page `screen` (PRINT_SCREENS) would show at `size` with
    `animal` (with its image settings, as with_image_settings gives) as its
    print instead of today's, drawn by that screen's own code: the
    dashboard with today's weather and snow, the daily view with its
    example planner data (labelled on the page). Returns (image, the Art
    drawn or None, the art box the print's placement is relative to, the
    box's _draw_art mode); box and mode are None for a page whose print
    can't be placed (panel_layout gives no layout)."""
    width, height = size
    if screen == "daily":
        today = date.today()
        img, art = _daily(width, height, animal, planner_source.get_planner(today), today)
        if panel_layout({"screen": screen}, size) is None:
            return img, art, None, None
        box, mode = art_box(width, height, DAILY_LAYOUT, False)
    elif screen == "dashboard":
        data = _dashboard_data(cfg, animal)
        img, art = _dashboard(width, height, cfg, data)
        box, mode = art_box(width, height, panel_layout({**cfg, "screen": screen}, size),
                            bool(data["snow"]))
    else:
        raise ValueError(f"no print on screen {screen!r}")
    return img, art, box, mode


def page_preview(screen: str, animal: dict | None, cfg: dict, size: tuple[int, int]):
    """_page as (image, art box or None, the placement as drawn after
    clamping; None for the automatic fit or a print that can't be placed)."""
    img, art, box, _ = _page(screen, animal, cfg, size)
    return img, box, art.placement if art else None


def page_layers(screen: str, animal: dict | None, cfg: dict, size: tuple[int, int],
                long_side: int) -> dict:
    """The page split in two for the browser to move the print live
    (DECISIONS §29), scaled so the long side is at most `long_side` px:
    - "page": RGBA, everything but the art — transparent where the art
      shows, partly opaque where a lightened patch or a halo lies over it.
    - "art": RGBA, the plate at its drawn placement and scale, not clipped
      to the box (up to a box's size beyond it each way, as far as a drag
      can reveal), its own edges faded as the mode fades them; with "rect"
      (where it lies) and "centre" (the subject's centre) in frame px.
      None when no print could be drawn.
    White, then the art clipped to the box, then the page, is the full
    render before dithering, give or take the fades the box's inner sides
    ("fades": side → frame px) add where the art is cut there.
    Also "box", "auto" (the automatic fit's subject centre, which a
    placement shifts from, frame px) and "placement" as drawn.
    ValueError for a page whose print can't be placed."""
    if screen == "daily" and panel_layout({"screen": screen}, size) is None:
        raise ValueError("the portrait daily view shows the print whole; it can't be placed")
    f = min(1.0, long_side / max(size))
    token = _ART_FILL.set(WHITE)
    try:
        on_white, art, box, mode = _page(screen, animal, cfg, size)
    finally:
        _ART_FILL.reset(token)
    token = _ART_FILL.set(BLACK)
    try:
        on_black = _page(screen, animal, cfg, size)[0]
    finally:
        _ART_FILL.reset(token)
    page = _unblend(on_white, on_black)
    if f < 1:
        page = page.resize((round(page.width * f), round(page.height * f)), Image.LANCZOS)
    X, Y = _scalers(*size, PORTRAIT if size[1] > size[0] else LANDSCAPE)
    out = {"page": page, "art": None, "box": list(box), "auto": None,
           "fades": box_fades(box, size, mode, X, Y),
           "placement": (art.placement if art else None) or dict(DEFAULT_PLACEMENT)}
    if art and art.geom:
        g = art.geom
        sw, sh = g.sb[2] - g.sb[0], g.sb[3] - g.sb[1]
        out["auto"] = [g.origin[0] + sw * g.scale / 2, g.origin[1] + sh * g.scale / 2]
        out["art"] = _art_layer(g, out["placement"], f)
    return out


def _unblend(on_white: Image.Image, on_black: Image.Image) -> Image.Image:
    """What was drawn over the art box, as RGBA, from the page drawn with
    the box white and with it black: where those differ by d the overlay's
    alpha is 255 - d, and its colour is the black version over that alpha."""
    from PIL import ImageChops, ImageMath
    alpha = ImageChops.invert(ImageChops.subtract(on_white, on_black).convert("L"))
    bands = [ImageMath.lambda_eval(
        lambda e: e["convert"](e["min"](e["b"] * 255 / e["max"](e["a"], 1), 255), "L"),
        a=alpha, b=band) for band in on_black.split()]
    return Image.merge("RGBA", (*bands, alpha))


def _art_layer(g: "ArtGeom", placement: dict, f: float) -> dict:
    """The plate at `placement`, unclipped by the art box, at `f` of frame
    scale: {"image" (RGBA), "rect", "centre", "scale"} with frame-px rect
    and centre, and the placement scale it was drawn at."""
    bx0, by0, bx1, by1 = g.box
    bw, bh = bx1 - bx0, by1 - by0
    sw, sh = g.sb[2] - g.sb[0], g.sb[3] - g.sb[1]
    ncx, ncy, s, _ = _placed_centre(g, placement)
    ox, oy = ncx - sw * s / 2, ncy - sh * s / 2   # the subject box's top-left
    ex, ey = g.X(_context(g.mode)) / s, g.Y(_context(g.mode)) / s
    # The sheet around the subject, as far as a drag of one box can bring
    # into view (sheet px).
    sx0 = max(0.0, g.sb[0] - ex, g.sb[0] + (bx0 - bw - ox) / s)
    sy0 = max(0.0, g.sb[1] - ey, g.sb[1] + (by0 - bh - oy) / s)
    sx1 = min(g.sheet.width, g.sb[2] + ex, g.sb[0] + (bx1 + bw - ox) / s)
    sy1 = min(g.sheet.height, g.sb[3] + ey, g.sb[1] + (by1 + bh - oy) / s)
    rect = [ox + (sx0 - g.sb[0]) * s, oy + (sy0 - g.sb[1]) * s,
            ox + (sx1 - g.sb[0]) * s, oy + (sy1 - g.sb[1]) * s]
    w = max(1, round((rect[2] - rect[0]) * f))
    h = max(1, round((rect[3] - rect[1]) * f))
    img = g.sheet.resize((w, h), Image.LANCZOS, box=(sx0, sy0, sx1, sy1)).convert("RGBA")
    if g.mode != "daily":
        # Its own edges fade as the dashboard fades the art's (the daily
        # view fades only at its box, which box_fades covers). Placed clear
        # of a pretend frame's border so every side fades.
        Xf, Yf = (lambda v: g.X(v) * f), (lambda v: g.Y(v) * f)
        img.putalpha(_art_mask(img, 1, 1, (0, 0, w, h), (w + 2, h + 2), g.mode, Xf, Yf))
    return {"image": img, "rect": [round(v, 2) for v in rect],
            "centre": [round(ncx, 2), round(ncy, 2)], "scale": placement["scale"]}


def box_fades(box, frame, mode: str, X, Y) -> dict:
    """The fades _art_mask gives art cut by the box's sides: {side: frame
    px} for each side of the box not on the frame's border."""
    bw, bh = box[2] - box[0], box[3] - box[1]
    fades = _fade_widths(mode, bw, bh, X, Y)
    on_border = {"left": box[0] <= 0, "top": box[1] <= 0,
                 "right": box[2] >= frame[0], "bottom": box[3] >= frame[1]}
    return {k: round(v, 1) for k, v in fades.items() if not on_border[k]}


def _scalers(width: int, height: int, base: tuple[int, int]):
    sx, sy = width / base[0], height / base[1]
    return (lambda v: int(v * sx)), (lambda v: int(v * sy))


def _hero_rows(height: int, Y, snow: bool) -> tuple[int, int]:
    """Hero, built upwards from the bottom border: (bottom, band_top) — the
    snow row (optional) sits on `bottom`, the weather band starts at
    `band_top`, and the art takes whatever is left above."""
    bottom = height - Y(52)
    snow_top = bottom - Y(HERO_SNOW_H) if snow else bottom
    band_top = (snow_top - Y(28) if snow else bottom) - Y(HERO_WEATHER_H)
    return bottom, band_top


def art_box(width: int, height: int, layout: str, snow: bool):
    """Where the dashboard draws the art for an art layout (ART_LAYOUTS) at
    this frame size and snow row: ((x0, y0, x1, y1), mode) for _draw_art."""
    if layout not in ART_LAYOUTS:
        raise ValueError(f"unknown art layout: {layout!r}")
    if layout == DAILY_LAYOUT:
        return _daily_art_box(width, height), "daily"   # landscape only
    if layout != LANDSCAPE_LAYOUT:
        X, Y = _scalers(width, height, PORTRAIT)
        if layout == "hero":
            _, band_top = _hero_rows(height, Y, snow)
            # Full bleed: the box runs to the top, left and right borders.
            return (0, 0, width, band_top - Y(24)), "centered"
        return (0, Y(500), width, height), "bleed"
    X, Y = _scalers(width, height, LANDSCAPE)
    # The caption clears the top; the weather only occupies the upper left,
    # so the box reaches under it — a wide plate's left edge is inside the
    # fade by then — and runs to the bottom border.
    return (X(440), Y(140), width, height), "bleed"


def _dashboard_landscape(width: int, height: int, cfg: dict, data: dict):
    img = Image.new("RGB", (width, height), WHITE)
    X, Y = _scalers(width, height, LANDSCAPE)
    halo = dict(stroke_width=max(2, Y(4)), stroke_fill=WHITE)   # text over art

    box, mode = art_box(width, height, LANDSCAPE_LAYOUT, bool(data["snow"]))
    drew_art = _draw_art(img, data["animal"], box, X, Y, mode=mode,
                         layout=LANDSCAPE_LAYOUT)

    _draw_snow_over_art(img, drew_art, X(52), data, cfg, X, Y, halo)
    d = ImageDraw.Draw(img)
    _draw_caption(d, data["animal"] if drew_art else None, width - X(48), Y(36),
                  X, Y, halo)
    _draw_weather(d, data["weather"], X(52), Y(44), X, Y, halo)
    return img, drew_art


def _draw_snow_over_art(img: Image.Image, art: "Art | None", x: int, data: dict,
                        cfg: dict, X, Y, halo) -> None:
    """The snow report along the bottom-left border. The art is opaque up
    to the border (full bleed), so where the report lands on it the art is
    lightened under it first, as the hero does for its caption."""
    if not data["snow"]:
        return
    bottom = img.height - Y(52)
    rect = _snow_report(ImageDraw.Draw(Image.new("RGB", (1, 1))), x, bottom,
                        data["snow"], X, Y, halo, name=cfg["snow_location_name"])
    if art and _overlaps(rect, art.rect):
        _fade_behind(img, rect, X, Y)
    _snow_report(ImageDraw.Draw(img), x, bottom, data["snow"], X, Y, halo,
                 name=cfg["snow_location_name"])


def _dashboard_portrait(width: int, height: int, cfg: dict, data: dict,
                        layout: str | None = None):
    layout = layout if layout in PORTRAIT_LAYOUTS else DEFAULT_PORTRAIT_LAYOUT
    img = Image.new("RGB", (width, height), WHITE)
    X, Y = _scalers(width, height, PORTRAIT)
    halo = dict(stroke_width=max(2, Y(4)), stroke_fill=WHITE)
    LX = X(52)
    right = width - X(48)

    if layout == "hero":
        bottom, band_top = _hero_rows(height, Y, bool(data["snow"]))
        box, mode = art_box(width, height, layout, bool(data["snow"]))
        art = _draw_art(img, data["animal"], box, X, Y, mode=mode, layout=layout)
        art_rect = art.rect if art else None
        d = ImageDraw.Draw(img)
        # Anything laid over the art is measured first (drawn into a 1px
        # scratch) so the art can be lightened under it before it is drawn.
        dry = ImageDraw.Draw(Image.new("RGB", (1, 1)))

        def over_art(rect):
            if art_rect and _overlaps(rect, art_rect):
                _fade_behind(img, rect, X, Y)

        if art_rect:
            f_cap = _font(Y(26), style="serif_italic")
            lines = _caption_lines(d, data["animal"], f_cap, X(CAPTION_WIDTH))
            cap_top = Y(HERO_MARGIN) + Y(6)
            cap_w = max(d.textlength(ln, font=f_cap) for ln in lines)
            over_art((int(right - cap_w), cap_top, right, cap_top + Y(34) * len(lines)))
            _draw_caption_lines(d, lines, f_cap, right, cap_top, Y, halo)
        cx = width // 2
        over_art(_draw_weather_centred(dry, data["weather"], cx, band_top, X, Y, halo))
        _draw_weather_centred(d, data["weather"], cx, band_top, X, Y, halo)
        if data["snow"]:
            _snow_foot(d, cx, bottom, data["snow"], X, Y, halo,
                       name=cfg["snow_location_name"])
        return img, art

    # stack: the landscape arrangement folded — info on top, art below.
    box, mode = art_box(width, height, layout, bool(data["snow"]))
    drew_art = _draw_art(img, data["animal"], box, X, Y, mode=mode, layout=layout)
    _draw_snow_over_art(img, drew_art, LX, data, cfg, X, Y, halo)
    d = ImageDraw.Draw(img)
    _draw_caption(d, data["animal"] if drew_art else None, right,
                  Y(PORTRAIT_CAPTION_TOP), X, Y, halo)
    _draw_weather(d, data["weather"], LX, Y(44), X, Y, halo)
    return img, drew_art


# ── dashboard widgets ─────────────────────────────────────────────────────────

def _ink_centroid(art: Image.Image) -> tuple[float, float]:
    """Centre of ink mass of a prepared plate, in its own pixels. The subject
    crop already bounds the figure; this is what centres it *visually* — a
    heron is a big body under a thin neck, and the eye centres the body."""
    mask, (sw, sh) = _ink_mask(art)
    sx = sy = n = 0
    for i, v in enumerate(mask.getdata()):
        if v:
            sx += i % sw
            sy += i // sw
            n += 1
    if not n:
        return art.width / 2, art.height / 2
    return (sx / n + 0.5) * art.width / sw, (sy / n + 0.5) * art.height / sh


def _ink_span(art: Image.Image) -> tuple[float, float, float, float]:
    """Where the ink starts and stops on each axis of a subject crop, in its
    own pixels: the part a lean-in crop must never cut."""
    mask, (sw, sh) = _ink_mask(art)
    cols, rows = _profiles(mask, (sw, sh))

    def span(prof):
        idx = [i for i, v in enumerate(prof) if v]
        return (idx[0], idx[-1] + 1) if idx else (0, len(prof))
    (x0, x1), (y0, y1) = span(cols), span(rows)
    return (x0 * art.width / sw, y0 * art.height / sh,
            x1 * art.width / sw, y1 * art.height / sh)


class ArtGeom(NamedTuple):
    """How _draw_art fitted a plate, before any placement: the prepared
    sheet, its subject box `sb` (sheet px), the automatic `scale`, and
    `origin`, where the scaled subject box's top-left lands in the frame.
    page_layers redraws the art from it at any placement."""
    sheet: Image.Image
    sb: tuple[int, int, int, int]
    scale: float
    origin: tuple[float, float]
    box: tuple[int, int, int, int]
    mode: str
    X: object
    Y: object


class Art(NamedTuple):
    rect: tuple[int, int, int, int]   # where the art was pasted, frame px
    placement: dict | None            # as drawn, after clamping; None = automatic
    geom: ArtGeom | None = None


# page_layers draws a page twice with the art's box filled with one colour
# (white, then black) instead of the art, to lift off what lies over it.
_ART_FILL: contextvars.ContextVar = contextvars.ContextVar("art_fill", default=None)


def _context(mode: str) -> int:
    """Panel px of sheet drawn around the subject: the hero's paper margin."""
    return HERO_CONTEXT if mode == "centered" else 0


def _draw_art(img: Image.Image, animal: dict | None, box, X, Y,
              mode: str = "bleed", layout: str | None = None) -> Art | None:
    """The engraving, fitted into a fixed art box (x0, y0, x1, y1) in frame
    pixels. The box is defined in panel coordinates, not derived from the
    image, so every plate lands in the same place whatever its dimensions.
    A figure is contained, then leaned into by up to ART_ZOOM where its
    subject box has paper to spare (the overflow is cut around the ink
    centroid, never through ink); a scene covers the box.
    `animal["rotation"]` (clockwise degrees, from config) turns a plate
    that was scanned sideways before anything else looks at it.
    `animal["placements"][layout]` (the print as dragged, for this art layout,
    a string the caller names; see PLACEMENT_SHIFT) then moves and scales
    the result inside the box; no entry draws it exactly as the automatic
    fit does.

    In every mode the art is opaque up to any frame border it reaches and
    fades only on its other edges (_art_mask).
    mode "bleed" (landscape, stack): anchored to the box's right edge with
    the last ART_BLEED running off the frame, long fades on the left and
    the top/bottom edges where they stop short of the border, so text can
    sit over it.
    mode "daily": full bleed — every plate covers the box (a figure too,
    cropped around its ink centroid), faded in only from the box's own
    inner left edge.
    mode "centered" (hero): the plate's ink centroid is put on the box's
    centre (clamped so nothing leaves the box) and drawn with up to
    HERO_CONTEXT of paper around it, so the soft HERO_FADE falls on that
    margin rather than the subject wherever the box has room — nothing
    overlaps it except its own caption.
    Returns the pasted rectangle and the placement drawn, or None if nothing
    was drawn."""
    if not animal:
        return None
    try:
        placement = normalize_placement(
            (animal.get("placements") or {}).get(layout) if layout else None)
    except ValueError as e:
        log.warning("ignoring bad placement for %s: %s", animal.get("file"), e)
        placement = None
    bx0, by0, bx1, by1 = box
    bw, bh = bx1 - bx0, by1 - by0
    try:
        plate = Image.open(animal["path"]).convert("RGB")
    except OSError:
        return None
    # A plate scanned sideways is turned first (PIL rotates anticlockwise,
    # the config says clockwise), so the crop and the fades see it upright.
    rot = int(animal.get("rotation") or 0) % 360
    if rot:
        plate = plate.rotate(-rot, expand=True)
    sheet, sb, is_scene = _prep_plate(plate)
    art = sheet.crop(sb)
    cover = is_scene or mode == "daily"
    if cover:
        scale = max(bw / art.width, bh / art.height)
    else:
        scale = min(bw / art.width, bh / art.height)
        # Keep the hero's soft edge on paper too, not on the drawing.
        ix = X(HERO_FADE) if mode == "centered" else 0
        iy = Y(HERO_FADE) if mode == "centered" else 0
        ink = _ink_span(art)
        scale = max(scale, min(scale * ART_ZOOM,
                               (bw - 2 * ix) / (ink[2] - ink[0]),
                               (bh - 2 * iy) / (ink[3] - ink[1])))
    art = art.resize((max(1, round(art.width * scale)),
                      max(1, round(art.height * scale))), Image.LANCZOS)
    if art.width > bw or art.height > bh:
        # Trim the overflow: a scene around its centre; a zoomed figure
        # around its ink centroid, but only ever through paper — the window
        # is clamped so the ink span stays whole.
        if cover:
            cx, cy = (art.width / 2, art.height / 2) if is_scene else _ink_centroid(art)
            lo_x = lo_y = 0
            hi_x, hi_y = art.width - bw, art.height - bh
        else:
            cx, cy = _ink_centroid(art)
            ix0, iy0, ix1, iy1 = (v * scale for v in ink)
            lo_x, hi_x = max(0, ix1 + ix - bw), min(art.width - bw, ix0 - ix)
            lo_y, hi_y = max(0, iy1 + iy - bh), min(art.height - bh, iy0 - iy)
        ox = round(max(lo_x, min(hi_x, cx - bw / 2))) if art.width > bw else 0
        oy = round(max(lo_y, min(hi_y, cy - bh / 2))) if art.height > bh else 0
        art = art.crop((ox, oy, ox + min(bw, art.width), oy + min(bh, art.height)))
    else:
        ox = oy = 0
    if mode == "centered":
        cx, cy = _ink_centroid(art)
        x = round(bx0 + bw / 2 - cx)
        y = round(by0 + bh / 2 - cy)
        x = max(bx0, min(bx1 - art.width, x))
        y = max(by0, min(by1 - art.height, y))
        origin = (x - ox, y - oy)
        art, x, y = _with_context(sheet, sb, scale, origin, box, X, Y)
    else:
        # A covering plate fills its box exactly, so it meets every border
        # the box does; a contained figure bleeds ART_BLEED off the right.
        x = bx1 - (art.width if cover else round(art.width * (1 - ART_BLEED)))
        y = by0 + (bh - art.height) // 2
        origin = (x - ox, y - oy)
    geom = ArtGeom(sheet, sb, scale, origin, box, mode, X, Y)
    drawn = None
    if placement:
        art, x, y, drawn = _place(geom, placement)
    fill = _ART_FILL.get()
    if fill is None:
        img.paste(art, (x, y), _art_mask(art, x, y, box, img.size, mode, X, Y))
    else:
        img.paste(fill, (max(0, bx0), max(0, by0), min(img.width, bx1), min(img.height, by1)))
    return Art((x, y, x + art.width, y + art.height), drawn, geom)


def _fade_widths(mode: str, w: float, h: float, X, Y) -> dict:
    """Each side's fade, frame px, for art (dashboard modes) or a box (the
    daily view's) of w x h."""
    if mode == "daily":
        return {"left": max(1.0, w * DAILY_FADE)}
    if mode == "centered":
        fx, fy = X(HERO_FADE), Y(HERO_FADE)
        fades = {"left": fx, "right": fx, "top": fy, "bottom": fy}
    else:
        fades = {"left": X(ART_FADE_LEFT), "right": X(ART_FADE_EDGE),
                 "top": Y(ART_FADE_EDGE), "bottom": Y(ART_FADE_EDGE)}
    return {k: min(v, 0.45 * (w if k in ("left", "right") else h)) for k, v in fades.items()}


def _art_mask(art: Image.Image, x: int, y: int, box, frame, mode: str, X, Y) -> Image.Image:
    """The edge fades for art pasted at (x, y) in its box on a frame of size
    `frame`. One rule for every layout: a side of the art that lies on the
    frame's border is opaque right up to it (full bleed); every other side
    fades in over a fixed panel width, not a share of the image (otherwise a
    small plate fades over a few pixels and a large one over hundreds, which
    is what made the peacock dissolve early).
    Dashboard art fades from its own edges: the hero's soft HERO_FADE hides
    a crop line, the bleed layouts' long left fade lets the weather sit
    over it. Daily art fades in only from its box's inner left edge, where
    it meets the calendar, wherever the art itself starts (DECISIONS §26);
    its other sides never fade, so a daily placement shrunk below the box
    shows hard edges there."""
    W, H = frame
    w, h = art.size
    if mode == "daily":
        fades = _fade_widths(mode, box[2] - box[0], box[3] - box[1], X, Y)
        lo, hi = (box[0], box[1]), (box[2], box[3])
    else:
        fades = _fade_widths(mode, w, h, X, Y)
        lo, hi = (x, y), (x + w, y + h)
    # A side on the frame's border never fades.
    if x <= 0:
        fades.pop("left", None)
    if y <= 0:
        fades.pop("top", None)
    if x + w >= W:
        fades.pop("right", None)
    if y + h >= H:
        fades.pop("bottom", None)

    def ramp(n: int, start: int, near: str, far: str, lo_ref: float, hi_ref: float):
        out = []
        for i in range(n):
            p, v = start + i, 1.0
            if near in fades:
                v = min(v, (p - lo_ref) / fades[near])
            if far in fades:
                v = min(v, (hi_ref - 1 - p) / fades[far])
            out.append(int(255 * max(0.0, min(1.0, v))))
        return out

    from PIL import ImageChops
    hx = Image.new("L", (w, 1))
    hx.putdata(ramp(w, x, "left", "right", lo[0], hi[0]))
    vy = Image.new("L", (1, h))
    vy.putdata(ramp(h, y, "top", "bottom", lo[1], hi[1]))
    return ImageChops.darker(hx.resize((w, h)), vy.resize((w, h)))


def _placed_centre(g: ArtGeom, placement: dict):
    """Where a placement puts the subject's centre (frame px) and at what
    scale: scaled about the automatic fit's centre, then shifted by a share
    of the box, the centre kept inside the box. Returns (cx, cy, scale,
    the placement as drawn after that clamp)."""
    bx0, by0, bx1, by1 = g.box
    bw, bh = bx1 - bx0, by1 - by0
    sw, sh = g.sb[2] - g.sb[0], g.sb[3] - g.sb[1]
    cx, cy = g.origin[0] + sw * g.scale / 2, g.origin[1] + sh * g.scale / 2
    ncx = max(bx0, min(bx1, cx + placement["x"] * bw))
    ncy = max(by0, min(by1, cy + placement["y"] * bh))
    drawn = {"x": round((ncx - cx) / bw, 4), "y": round((ncy - cy) / bh, 4),
             "scale": placement["scale"]}
    return ncx, ncy, g.scale * placement["scale"], drawn


def _place(g: ArtGeom, placement: dict):
    """Apply a saved placement to art the automatic fit put at g.origin and
    g.scale. The pixels are resampled from the sheet, not from the fitted
    bitmap, so a zoom stays sharp. Returns (image, x, y, placement as
    drawn)."""
    sw, sh = g.sb[2] - g.sb[0], g.sb[3] - g.sb[1]
    ncx, ncy, s, drawn = _placed_centre(g, placement)
    art, x, y = _with_context(g.sheet, g.sb, s, (ncx - sw * s / 2, ncy - sh * s / 2),
                              g.box, g.X, g.Y, context=_context(g.mode))
    return art, x, y, drawn


def _with_context(sheet: Image.Image, sb, scale: float, origin, box, X, Y,
                  context: int = HERO_CONTEXT):
    """The subject plus up to `context` panel px of the sheet around it,
    clipped to the art box. `origin` is where the scaled subject box's
    top-left lands in the frame. Returns (image, x, y)."""
    ex, ey = X(context) / scale, Y(context) / scale   # in sheet px
    sx0 = max(0.0, sb[0] - ex, sb[0] + (box[0] - origin[0]) / scale)
    sy0 = max(0.0, sb[1] - ey, sb[1] + (box[1] - origin[1]) / scale)
    sx1 = min(sheet.width, sb[2] + ex, sb[0] + (box[2] - origin[0]) / scale)
    sy1 = min(sheet.height, sb[3] + ey, sb[1] + (box[3] - origin[1]) / scale)
    x = max(box[0], round(origin[0] + (sx0 - sb[0]) * scale))
    y = max(box[1], round(origin[1] + (sy0 - sb[1]) * scale))
    w = min(box[2], round(origin[0] + (sx1 - sb[0]) * scale)) - x
    h = min(box[3], round(origin[1] + (sy1 - sb[1]) * scale)) - y
    art = sheet.resize((max(1, w), max(1, h)), Image.LANCZOS,
                       box=(sx0, sy0, sx1, sy1))
    return art, x, y


def _overlaps(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _fade_behind(img: Image.Image, rect, X, Y) -> None:
    """Lighten the image under `rect` (x0, y0, x1, y1): a rounded, feathered
    white patch at CAPTION_FADE_STRENGTH, so a caption laid over the art
    reads without the art disappearing around it."""
    from PIL import ImageFilter
    pad, feather = X(CAPTION_FADE_PAD), X(CAPTION_FADE_FEATHER)
    x0, y0, x1, y1 = rect
    # Work on a crop: a full-frame blur is needless work.
    reach = pad + feather * 3
    cx0, cy0 = max(0, x0 - reach), max(0, y0 - reach)
    cx1, cy1 = min(img.width, x1 + reach), min(img.height, y1 + reach)
    m = Image.new("L", (cx1 - cx0, cy1 - cy0), 0)
    ImageDraw.Draw(m).rounded_rectangle(
        [x0 - pad - cx0, y0 - pad - cy0, x1 + pad - cx0, y1 + pad - cy0],
        radius=pad, fill=int(255 * CAPTION_FADE_STRENGTH))
    m = m.filter(ImageFilter.GaussianBlur(feather))
    img.paste(WHITE, (cx0, cy0, cx1, cy1), m)


def _caption_parts(animal: dict, name: bool = True) -> list[str]:
    """The caption's ideas: "Rhinoceros · Rhinoceros unicornis", "of South
    Asia", "as depicted by Conrad Gessner, Zürich, 1551". name=False leaves
    the common name out of the first (a layout that sets it as a heading)."""
    line1 = animal.get("common_name", "?").capitalize() if name else ""
    if animal.get("sci_name"):
        line1 = f"{line1} · {animal['sci_name']}" if line1 else animal["sci_name"]
    artist, place, year = (animal.get("artist"), animal.get("place"),
                           animal.get("year"))
    if artist:
        clause = f"as depicted by {artist}" + (f", {place}" if place else "")
    elif place:
        clause = f"as depicted in {place}"
    else:
        clause = "drawn" if year else None
    if clause and year:
        clause += f", {year}"
    rest = [f"of {animal['native']}" if animal.get("native") else None, clause]
    return [p for p in [line1, *rest] if p]


def _caption_lines(d: ImageDraw.ImageDraw, animal: dict, font, max_w: int,
                   max_lines: int = 4) -> list[str]:
    """Source caption lines: "Rhinoceros · Rhinoceros unicornis /
    of South Asia / as depicted by Conrad Gessner, Zürich, 1551". One idea
    per line, each wrapped at max_w."""
    lines = []
    for part in _caption_parts(animal):
        lines += _wrap(d, part, font, max_w)
    return lines[:max_lines]


def _draw_caption_lines(d: ImageDraw.ImageDraw, lines: list[str], font, right: int,
                        top: int, Y, halo, line_h: int = 34) -> None:
    cy = top
    for ln in lines:
        d.text((right - d.textlength(ln, font=font), cy), ln, font=font,
               fill=BLACK, **halo)
        cy += Y(line_h)


def _draw_caption(d: ImageDraw.ImageDraw, animal: dict | None, right: int, top: int,
                  X, Y, halo, max_w: int | None = None, max_lines: int = 4) -> int:
    """Right-aligned source caption at (right, top), wrapped narrow enough to
    stay clear of the weather block's HIGH column. Returns lines drawn."""
    if not animal:
        return 0
    f_cap = _font(Y(26), style="serif_italic")
    lines = _caption_lines(d, animal, f_cap, max_w or X(CAPTION_WIDTH), max_lines)
    _draw_caption_lines(d, lines, f_cap, right, top, Y, halo)
    return len(lines)


def _draw_weather(d: ImageDraw.ImageDraw, weather: dict | None, LX: int, top: int,
                  X, Y, halo, spread: int = 0):
    """Everything flush to one left edge: today's forecast glyph (what the
    day will do — the window shows what it's doing now) beside the current
    temperature and today's high; sunrise/sunset; then the next two days.
    `top` is the top of the temperature; the block is WEATHER_BLOCK_H tall,
    plus 2*spread when the sunrise and forecast rows are pushed apart.
    Returns the block's rectangle."""
    def P(v):   # layout rows were drawn at absolute panel y's from 44 down
        return top + Y(v - 44) + Y(spread * (2 if v >= 340 else 1 if v >= 250 else 0))

    f_big = _font(Y(40), style="display")
    if not (weather and weather.get("days")):
        d.text((LX, P(100)), "weather unavailable", font=f_big, fill=RED, **halo)
        return (LX, P(100), LX + int(d.textlength("weather unavailable", font=f_big)),
                P(100) + Y(44))
    today = weather["days"][0]
    kind = datasources.icon_kind(today["code"])
    icon_r = Y(62)
    icon_w = _weather_icon_width(d, icon_r, kind)
    _weather_icon(d, LX + icon_w // 2, P(132), icon_r, kind)
    x = LX + icon_w + X(28)
    x += _temp_cf(d, x, P(44), weather["current_c"], Y(130), BLACK, halo) + X(46)
    f_lab = _font(Y(20), style="display")
    d.text((x, P(62)), "HIGH", font=f_lab, fill=BLACK, **halo)
    x_end = x + _temp_cf(d, x, P(92), today["tmax"], Y(64), RED, halo)

    f_t = _font(Y(30), style="display")
    gsz = Y(60)
    gw = _glyph_width(d, WI_SUNRISE, gsz)
    x = LX
    for txt, rising in ((_hhmm(today["sunrise"]), True),
                        (_hhmm(today["sunset"]), False)):
        _sun_horizon(d, x + gw // 2, P(266), gsz, rising)
        d.text((x + gw + X(10), P(250)), txt, font=f_t, fill=BLACK, **halo)
        x += gw + X(10) + d.textlength(txt, font=f_t) + X(44)

    f_day = _font(Y(28), style="display")
    for i, day in enumerate(weather["days"][1:3]):
        cx0 = LX + i * X(256)
        dk = datasources.icon_kind(day["code"])
        iw = _weather_icon_width(d, Y(30), dk)
        _weather_icon(d, cx0 + iw // 2, P(356), Y(30), dk)
        d.text((cx0 + iw + X(12), P(340)), _dow(day["date"]), font=f_day,
               fill=BLACK, **halo)
        # Low then high, both units.
        lo, hi = str(round(day["tmin"])), str(round(day["tmax"]))
        w_lo = _deg(d, cx0, P(396), lo, Y(38), BLUE, halo=halo)
        _deg(d, cx0 + w_lo + X(18), P(396), hi, Y(38), RED, halo=halo)
        d.text((cx0, P(452)),
               f"{_c_to_f(day['tmin'])} / {_c_to_f(day['tmax'])} °F",
               font=_font(Y(22)), fill=BLACK, **halo)
    return (LX, top, max(x_end, LX + X(2 * 256)), P(452) + Y(28))


def _draw_weather_centred(d: ImageDraw.ImageDraw, weather: dict | None, cx: int,
                          top: int, X, Y, halo):
    """The hero band: today's forecast glyph, current temperature and HIGH
    centred as one row; under it two grouped blocks hugging ±HERO_BAND_HALF
    of centre — the next two days on the left, sunrise over sunset on the
    right. HERO_WEATHER_H tall. Returns the band's rectangle."""
    left, right = cx - X(HERO_BAND_HALF), cx + X(HERO_BAND_HALF)
    rect = (left, top, right, top + Y(HERO_WEATHER_H))
    if not (weather and weather.get("days")):
        f = _font(Y(40), style="display")
        txt = "weather unavailable"
        d.text((cx - d.textlength(txt, font=f) / 2, top + Y(60)), txt, font=f,
               fill=RED, **halo)
        return rect
    today = weather["days"][0]

    # Row 1, centred: glyph · 21° · HIGH 28°
    kind = datasources.icon_kind(today["code"])
    iw = _weather_icon_width(d, Y(58), kind)
    w_cur = _temp_cf(d, 0, 0, weather["current_c"], Y(124), BLACK, halo, measure=True)
    w_hi = _temp_cf(d, 0, 0, today["tmax"], Y(60), RED, halo, measure=True)
    x = cx - (iw + X(28) + w_cur + X(44) + w_hi) // 2
    _weather_icon(d, x + iw // 2, top + Y(84), Y(58), kind)
    x += iw + X(28)
    x += _temp_cf(d, x, top, weather["current_c"], Y(124), BLACK, halo) + X(44)
    d.text((x, top + Y(16)), "HIGH", font=_font(Y(20), style="display"), fill=BLACK, **halo)
    _temp_cf(d, x, top + Y(46), today["tmax"], Y(60), RED, halo)

    # Row 2, left block: the next two days (glyph + weekday, low / high, °F).
    ry = top + Y(206)
    f_day = _font(Y(28), style="display")
    x = left
    for day in weather["days"][1:3]:
        dk = datasources.icon_kind(day["code"])
        dw = _weather_icon_width(d, Y(28), dk)
        _weather_icon(d, x + dw // 2, ry + Y(16), Y(28), dk)
        d.text((x + dw + X(12), ry), _dow(day["date"]), font=f_day, fill=BLACK, **halo)
        lo, hi = str(round(day["tmin"])), str(round(day["tmax"]))
        wl = _deg(d, x, ry + Y(52), lo, Y(36), BLUE, halo=halo)
        _deg(d, x + wl + X(16), ry + Y(52), hi, Y(36), RED, halo=halo)
        d.text((x, ry + Y(104)), f"{_c_to_f(day['tmin'])} / {_c_to_f(day['tmax'])} °F",
               font=_font(Y(20)), fill=BLACK, **halo)
        x += X(210)

    # Row 2, right block: sunrise over sunset, times flush right, glyphs in
    # a column (both times measured so the glyphs line up).
    f_t = _font(Y(28), style="display")
    gsz = Y(56)
    gw = _glyph_width(d, WI_SUNRISE, gsz)
    times = ((_hhmm(today["sunrise"]), True), (_hhmm(today["sunset"]), False))
    tw = max(d.textlength(t, font=f_t) for t, _ in times)
    gx = right - tw - X(12) - gw // 2
    for i, (txt, rising) in enumerate(times):
        yy = ry + i * Y(66)
        _sun_horizon(d, gx, yy + Y(16), gsz, rising)
        d.text((right - d.textlength(txt, font=f_t), yy), txt, font=f_t, fill=BLACK, **halo)
    return rect


def _snow_foot(d: ImageDraw.ImageDraw, cx: int, bottom: int, snow, X, Y, halo,
               name: str = ""):
    """The hero snow row, centred on cx with its bottom at `bottom`: base and
    72 h figures, then the three coming days as forecast glyph with the cm
    amount (blue when heavy) over the weekday; the location label centred
    above with clear air under it. HERO_SNOW_H tall. Returns its rectangle."""
    f_big = _font(Y(36), style="display")
    f_amt = _font(Y(28), style="display")
    f_s = _font(Y(19), style="display")
    depth, recent = _cm(snow.get("depth_cm")), _cm(snow.get("recent_cm"))
    items = [(None, str(depth), "CM BASE", 0), (None, f"+{recent}", "CM · 3D", 0)]
    for dy in (snow.get("days") or [])[:3]:
        cm = _cm(dy.get("snow_cm"))
        items.append((datasources.icon_kind(dy.get("code")), f"{cm}cm",
                      _dow(dy.get("date")).upper(), cm))
    widths = []
    for k, big, lab, _ in items:
        fw = f_big if k is None else f_amt
        widths.append((_weather_icon_width(d, Y(22), k) + X(6) if k else 0)
                      + max(d.textlength(big, font=fw), d.textlength(lab, font=f_s)))
    total = sum(widths) + X(44) * (len(items) - 1)
    fy = bottom - Y(60)             # top of the figures line
    x = cx - total // 2
    top = fy - Y(6)
    if name:
        top = fy - Y(52)
        d.text((cx - d.textlength(name, font=f_s) / 2, top), name, font=f_s,
               fill=BLACK, **halo)
    for (k, big, lab, cm), w in zip(items, widths):
        xx = x
        if k:
            iw = _weather_icon_width(d, Y(22), k)
            _weather_icon(d, xx + iw // 2, fy + Y(16), Y(22), k)
            xx += iw + X(6)
            d.text((xx, fy), big, font=f_amt,
                   fill=BLUE if cm >= SNOW_HEAVY_CM else BLACK, **halo)
        else:
            d.text((xx, fy - Y(8)), big, font=f_big, fill=BLACK, **halo)
        d.text((xx, fy + Y(32)), lab, font=f_s, fill=BLACK, **halo)
        x += w + X(44)
    return (cx - total // 2, top, cx + total // 2, bottom)


# ── dashboard helpers: temperatures, snow report ─────────────────────────────

CAPTION_WIDTH = 520   # top-right animal caption; wider collides with HIGH


SNOW_FLAKE = 0xf076   # wi-snowflake-cold
SNOW_HEAVY_CM = 5     # a day at or above this is drawn in blue ("go skiing")


def _dow(iso: str) -> str:
    """'2026-12-16' -> 'Wed'. Day abbreviations everywhere, never TODAY/TOMORROW."""
    try:
        return date.fromisoformat(iso).strftime("%a")
    except (TypeError, ValueError):
        return "?"


def _temp_cf(d: ImageDraw.ImageDraw, x: int, y: int, c: float, size: int, fill,
             halo: dict, measure: bool = False) -> int:
    """Big °C with the °F tucked small at its bottom-right. Returns width."""
    big = str(round(c))
    f_size = max(8, int(size * 0.30))
    small = str(_c_to_f(c))
    w_c = _deg(d, x, y, big, size, fill, measure=True)
    w_f = _deg(d, 0, 0, small, f_size, RED if fill == BLACK else fill, suffix="F",
               measure=True)
    pad = int(size * 0.04)
    if measure:
        return w_c + pad + w_f
    _deg(d, x, y, big, size, fill, halo=halo)
    bottom = d.textbbox((x, y), big, font=_font(size, style="display"))[3]
    fy = bottom - d.textbbox((0, 0), small, font=_font(f_size, style="display"))[3]
    _deg(d, x + w_c + pad, fy, small, f_size, RED if fill == BLACK else fill,
         suffix="F", halo=halo)
    return w_c + pad + w_f


def _cm(v) -> int:
    return round(v or 0)


def _snow_report(d, x, bottom, snow, X, Y, halo, name: str = "", align: str = "left"):
    """Location label, snowpack row (base, +72 h with a small red "3d" range
    tag), then a row of the coming days: forecast glyph, with the cm amount
    over the weekday centred on it. Heavy days (≥ SNOW_HEAVY_CM) in blue.
    `bottom` is the bottom edge, so it always sits just above whatever is
    below it; `x` is the left edge, or the right edge with align="right"
    (every row is then flush right). Returns the block's rectangle."""
    f_big = _font(Y(52), style="display")
    f_lab = _font(Y(19), style="display")
    f_amt = _font(Y(30), style="display")
    f_tag = _font(Y(20), style="display")
    depth, recent = _cm(snow.get("depth_cm")), _cm(snow.get("recent_cm"))
    right = align == "right"

    # Glyph row (bottom): glyph | amount over weekday, tight.
    gy = bottom - Y(30)                       # glyph centre line
    days = []
    for dy in (snow.get("days") or [])[:3]:
        cm = _cm(dy.get("snow_cm"))
        k = datasources.icon_kind(dy.get("code"))
        amt, dw = f"{cm}cm", _dow(dy.get("date")).upper()
        tw = max(d.textlength(amt, font=f_amt), d.textlength(dw, font=f_lab))
        days.append((k, _weather_icon_width(d, Y(26), k), amt, dw, tw, cm))
    row1_w = sum(iw + X(8) + tw for _, iw, _, _, tw, _ in days) + X(26) * max(0, len(days) - 1)
    x1 = x - row1_w if right else x
    for k, iw, amt, dw, tw, cm in days:
        _weather_icon(d, x1 + iw // 2, gy, Y(26), k)
        x1 += iw + X(8)
        # amount + label stacked, the pair's centre on the glyph's centre
        h_amt = d.textbbox((0, 0), amt, font=f_amt)[3]
        h_dw = d.textbbox((0, 0), dw, font=f_lab)[3]
        gap = Y(4)
        ty = gy - (h_amt + gap + h_dw) // 2
        d.text((x1, ty), amt, font=f_amt,
               fill=BLUE if cm >= SNOW_HEAVY_CM else BLACK, **halo)
        d.text((x1, ty + h_amt + gap), dw, font=f_lab, fill=BLACK, **halo)
        x1 += tw + X(26)

    # Snowpack row (above): big figures, a little air, then the small label.
    y2 = gy - Y(48) - Y(98)
    groups = []
    for big, tag, lab in ((str(depth), None, f"CM BASE · {round(depth / 2.54)} IN"),
                          (f"+{recent}", "3d", f"CM · {round(recent / 2.54)} IN")):
        w = d.textlength(big, font=f_big)
        if tag:
            w += X(6) + d.textlength(tag, font=f_tag)
        groups.append((big, tag, lab, w, max(w, d.textlength(lab, font=f_lab))))
    row2_w = sum(gw for *_, gw in groups) + X(48) * (len(groups) - 1)
    x2 = x - row2_w if right else x
    for big, tag, lab, w, gw in groups:
        d.text((x2, y2), big, font=f_big, fill=BLACK, **halo)
        if tag:
            d.text((x2 + d.textlength(big, font=f_big) + X(6), y2 + Y(4)), tag,
                   font=f_tag, fill=RED, **halo)
        d.text((x2, y2 + Y(66)), lab, font=f_lab, fill=BLACK, **halo)
        x2 += gw + X(48)

    # Location label on top, same size as the row labels.
    top = y2
    name_w = 0
    if name:
        top = y2 - Y(30)
        name_w = d.textlength(name, font=f_lab)
        d.text((x - name_w if right else x, top), name, font=f_lab, fill=BLACK, **halo)
    w = int(max(row1_w, row2_w, name_w))
    return (x - w, top, x, bottom) if right else (x, top, x + w, bottom)


# ── daily ─────────────────────────────────────────────────────────────────────
# This week's calendar and the todo list beside the daily animal. No clock
# and no date heading: the frame changes only when its content does. Inks
# and the faded past, see DECISIONS §26.

# owner → (outline colour, letter): the letter is always drawn, so an owner
# is never told by colour alone.
DAILY_OWNERS = {"m": (BLUE, "M"), "k": (RED, "K"), "mk": (YELLOW, "MK")}
# Days before today and done todos are drawn in this ink: a light grey that
# dithers to a fine black-on-paper stipple, so the text reads as faded but
# its letterforms hold (DECISIONS §26).
DAILY_FADED = (110, 110, 110)
DAILY_FADE = 0.15          # the art fades in over this share of its box
DAILY_LANDSCAPE_ART_X = 1060
DAILY_LIST_X = 620         # landscape: the todo list's left edge
DAILY_LIST_GAP = 28        # portrait: between the calendar and the list
DAILY_MARGIN = 40
DAILY_OPEN_PER_OWNER = 2   # open todos shown per owner (m, k, mk) ...
DAILY_DONE_MAX = 3         # ... then at most this many done, newest first
DAILY_DAY_MAX_EVENTS = 4   # lines per day before it is squeezed to fit
DAILY_EVENT_ROW_H = 36
DAILY_TODO_ROW_H = 58


# Portrait (DECISIONS §30): the calendar fills the left half, the todo list
# sits at the bottom right, and the print hangs between it and the caption
# as a polaroid on twine, shown whole. Layout px of the 1200x1600 frame.
DAILY_CAL_RIGHT = 572
DAILY_TODO_X = 632
DAILY_TODO_RIGHT = 1168
POLAROID_CX = 900          # the card's centre
POLAROID_BOTTOM = 1000     # the card's lowest point (and above the list)
POLAROID_CLEAR = 30        # below the caption, above the todo list
POLAROID_PHOTO_MAX_W = 500
POLAROID_SIDE, POLAROID_TOP, POLAROID_FOOT = 20, 34, 80   # the card's borders
POLAROID_TILT = -1.5       # degrees, clockwise positive (the mockup's sense)
POLAROID_INK = (46, 42, 39)          # #2e2a27: card outline, hole rims
POLAROID_HOLE = (232, 228, 223)      # #e8e4df
POLAROID_HOLE_R = 4.5
POLAROID_HOLE_IN = 11                # hole centres, inside the top and bottom edges
POLAROID_SHADOW = ((4, 6), 9, 0.20)  # offset, blur, opacity
TWINE_OUTLINE, TWINE_CORE, TWINE_PLY = (90, 58, 29), (201, 159, 100), (134, 89, 45)
TWINE_TOP_X = 650          # where the twine comes in over the top border
TWINE_SS = 2               # supersampling for the card and the twine


def _daily_art_box(width: int, height: int):
    """The landscape daily view's art box: the right third, full height.
    The portrait view has none: its print hangs whole as a polaroid."""
    if height > width:
        raise ValueError("the portrait daily view has no art box")
    X, _ = _scalers(width, height, LANDSCAPE)
    return (X(DAILY_LANDSCAPE_ART_X), 0, width, height)


def _week(today: date) -> list[date]:
    """Monday to Sunday of today's week. `today` is local (the container's
    TZ, DECISIONS §11)."""
    monday = today - timedelta(days=today.weekday())
    return [monday + timedelta(days=i) for i in range(7)]


def _todo_rows(todos: dict) -> list[tuple[dict, bool]]:
    """The list's rows as (item, done): up to DAILY_OPEN_PER_OWNER open
    items per owner, interleaved M, K, MK, then the DAILY_DONE_MAX most
    recently done."""
    by_owner = {o: [t for t in todos.get("open") or [] if t.get("owner") == o]
                [:DAILY_OPEN_PER_OWNER] for o in DAILY_OWNERS}
    open_rows = [by_owner[o][i] for i in range(DAILY_OPEN_PER_OWNER)
                 for o in DAILY_OWNERS if i < len(by_owner[o])]
    done = sorted(todos.get("done") or [], key=lambda t: t.get("date") or "",
                  reverse=True)[:DAILY_DONE_MAX]
    return [(t, False) for t in open_rows] + [(t, True) for t in done]


def _spaced(d: ImageDraw.ImageDraw, x: int, y: int, text: str, font, spacing: int,
            fill, measure: bool = False) -> int:
    """Letter-spaced text; returns its width."""
    w = 0
    for i, ch in enumerate(text):
        if not measure:
            d.text((x + w, y), ch, font=font, fill=fill)
        w += d.textlength(ch, font=font) + (spacing if i < len(text) - 1 else 0)
    return int(w)


def _owner_tag(d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, owner: str,
               font, outline: int = 3, faded: bool = False) -> None:
    """A box outlined in the owner's colour with the letter inside, no fill.
    Yellow is too pale to hold a thin line on its own, so its box gets a
    one-pixel black edge outside. A faded tag keeps its colour, at half the
    line weight, with a faded letter. An unknown owner gets no tag (and a
    warning) rather than a made-up one."""
    if owner not in DAILY_OWNERS:
        log.warning("daily: unknown owner %r, drawing no tag", owner)
        return
    colour, letter = DAILY_OWNERS[owner]
    if faded:
        outline = max(1, outline // 2)
    if colour == YELLOW:
        d.rectangle([x - 1, y - 1, x + w, y + h], outline=BLACK, width=1)
    d.rectangle([x, y, x + w - 1, y + h - 1], fill=WHITE, outline=colour, width=outline)
    _centered_text(d, (x + w / 2, y + h / 2), letter, font, DAILY_FADED if faded else BLACK)


def _fit(d: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    """`text`, cut with an ellipsis to fit max_w."""
    if max_w <= 0 or d.textlength(text, font=font) <= max_w:
        return text
    while text and d.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def _day_lines(events: list[dict], cap: int) -> list[dict | str]:
    """A day's events in reading order (all-day, then timed, then untimed),
    at most `cap` lines: past it the last line says how many more."""
    evs = sorted(events, key=lambda e: (0 if e.get("all_day") else 1 if e.get("time") else 2,
                                        e.get("time") or ""))
    if len(evs) <= cap:
        return evs
    return evs[:cap - 1] + [f"+{len(evs) - cap + 1} more"]


class _CalStyle(NamedTuple):
    """The calendar's sizes, in layout px (scaled by X/Y when drawn)."""
    num: int           # date number (bold)
    weekday: int       # weekday label, letter-spaced by weekday_track
    weekday_track: int
    weekday_gap: int   # the weekday label's top, below the date number's top
    text: int          # time and title
    allday: int        # ALL DAY small caps, letter-spaced by allday_track
    allday_track: int
    tag_font: int
    tag_w: int
    tag_h: int
    row: int           # one event line
    time_x: int        # the time column, right of the calendar's left edge
    title_lines: int   # a title wraps to at most this many lines ...
    wrap_h: int        # ... each further line this much lower
    spread: bool       # spare height goes into equal gaps between days


# Landscape: compact, beside the list, titles cut to one line.
CAL_COMPACT = _CalStyle(num=42, weekday=17, weekday_track=2, weekday_gap=40, text=26,
                        allday=19, allday_track=1, tag_font=20, tag_w=48, tag_h=32,
                        row=DAILY_EVENT_ROW_H, time_x=84, title_lines=1, wrap_h=0,
                        spread=False)
# Portrait: the whole left half, so larger type, titles wrapped to two lines
# and the days spread down the full height (DECISIONS §30).
CAL_TALL = _CalStyle(num=56, weekday=18, weekday_track=2, weekday_gap=56, text=28,
                     allday=19, allday_track=2, tag_font=20, tag_w=50, tag_h=34,
                     row=50, time_x=92, title_lines=2, wrap_h=34, spread=True)


def _draw_calendar(d: ImageDraw.ImageDraw, events: list[dict], today: date, x: int,
                   top: int, bottom: int, title_right: int, X, Y, avoid=None,
                   style: _CalStyle = CAL_COMPACT) -> int:
    """The week, one block per day: date and weekday in a left column, the
    date's top level with the cap height of the day's first line, then a
    line per event — time (its own column; ALL DAY in small caps; blank
    when untimed), owner tag, title. Days before today are drawn faded;
    today and later at full strength. Titles stop at `title_right`, or
    short of the `avoid` rectangle on the lines beside it; a style with
    title_lines > 1 wraps them, the last line cut with an ellipsis. A block
    is never shorter than its date column. When the week doesn't fit above
    `bottom`, titles are first kept to one line, then days squeezed to
    fewer lines each ("+N more"). A spreading style shares the spare
    height out as equal gaps between days. Returns its bottom."""
    f_num = _font(Y(style.num), bold=True)
    f_wd = _font(Y(style.weekday))
    f_time = _font(Y(style.text))
    f_allday = _font(Y(style.allday))
    f_tag = _font(Y(style.tag_font))
    f_title = _font(Y(style.text))
    halo = dict(stroke_width=max(1, Y(3)), stroke_fill=WHITE)
    week = _week(today)
    by_day = {dy.isoformat(): [e for e in events if e.get("date") == dy.isoformat()]
              for dy in week}
    pad, min_gap, row = Y(6), Y(10), Y(style.row)
    # Where a line's capitals start, below the top of its row: the date
    # number's top sits there too, whether or not the day has a line.
    cap_top = d.textbbox((0, row // 2), "H", font=f_title, anchor="lm")[1]
    wd_y = Y(style.weekday_gap)      # the weekday label, below the date's top
    date_col = cap_top + wd_y + d.textbbox((0, 0), "MTWFS", font=f_wd)[3]

    time_x = x + X(style.time_x)
    time_w = max(d.textlength("00:00", font=f_time),
                 _spaced(d, 0, 0, "ALL DAY", f_allday, X(style.allday_track), None,
                         measure=True))
    tag_x = time_x + int(time_w) + X(12)
    tag_w, tag_h = X(style.tag_w), Y(style.tag_h)
    title_x = tag_x + tag_w + X(12)

    def title_lines(ev, ly, n):
        right = title_right
        if avoid and ly < avoid[3] and ly + row > avoid[1]:
            right = min(right, avoid[0] - X(16))
        text = ev.get("title") or ""
        lines = _wrap(d, text, f_title, right - title_x) if n > 1 else [text]
        if len(lines) > n:
            lines = lines[:n - 1] + [" ".join(lines[n - 1:])]
        return [_fit(d, ln, f_title, right - title_x) for ln in lines]

    if avoid and style.spread:
        # avoid is checked against each line's place at the minimum gap.
        raise ValueError("a spreading calendar style cannot avoid a rectangle")

    def layout(cap, n):
        """Per day: its lines, each (event or "+N more", title lines, y
        within the day), and its height."""
        days, day_top = [], top
        for dy in week:
            ly, lines = 0, []
            for ev in _day_lines(by_day[dy.isoformat()], cap):
                tl = [ev] if isinstance(ev, str) else title_lines(ev, day_top + pad + ly, n)
                lines.append((ev, tl, ly))
                ly += row + (len(tl) - 1) * Y(style.wrap_h)
            h = 2 * pad + max(date_col, ly)
            days.append((lines, h))
            day_top += h + min_gap
        return days

    def fits(days):
        return top + sum(h for _, h in days) + min_gap * 6 <= bottom
    tries = [(DAILY_DAY_MAX_EVENTS, style.title_lines)]
    if style.title_lines > 1:
        tries.append((DAILY_DAY_MAX_EVENTS, 1))
    tries += [(c, 1 if style.title_lines > 1 else style.title_lines)
              for c in range(DAILY_DAY_MAX_EVENTS - 1, 0, -1)]
    for cap, n in tries:
        days = layout(cap, n)
        if fits(days):
            break
    gap = min_gap
    if style.spread:
        gap = max(min_gap, (bottom - top - sum(h for _, h in days)) // 6)

    y = top
    for dy, (lines, h) in zip(week, days):
        past = dy < today
        ink = DAILY_FADED if past else BLACK
        num = str(dy.day)
        num_top = y + pad + cap_top
        d.text((x, num_top - d.textbbox((0, 0), num, font=f_num)[1]), num, font=f_num, fill=ink)
        _spaced(d, x + X(1), num_top + wd_y, dy.strftime("%a").upper(), f_wd,
                X(style.weekday_track), ink)
        for ev, tl, ly in lines:
            mid = y + pad + ly + row // 2
            if isinstance(ev, str):        # "+N more"
                d.text((title_x, mid), ev, font=f_title, fill=ink, anchor="lm")
                continue
            if ev.get("all_day"):
                _spaced(d, time_x, mid - f_allday.size // 2 - Y(1), "ALL DAY",
                        f_allday, X(style.allday_track), ink)
            elif ev.get("time"):
                d.text((time_x, mid), ev["time"], font=f_time, fill=ink, anchor="lm")
            _owner_tag(d, tag_x, mid - tag_h // 2, tag_w, tag_h, ev.get("owner"), f_tag,
                       outline=max(1, X(3)), faded=past)
            for i, ln in enumerate(tl):
                d.text((title_x, mid + i * Y(style.wrap_h)), ln, font=f_title, fill=ink,
                       anchor="lm", **halo)
        y += h + gap
    return y - gap


def _draw_todos(d: ImageDraw.ImageDraw, todos: dict, x: int, top: int, width: int,
                bottom: int, X, Y) -> None:
    """One list, every row the same height: tag, text. Open items first,
    then done ones drawn faded, the text and its date struck through. Rows
    that don't fit above `bottom` are dropped from the end (done first)."""
    f_tag = _font(Y(24))
    f_txt = _font(Y(28))
    row = Y(DAILY_TODO_ROW_H)
    rows = _todo_rows(todos)[:max(0, (bottom - top) // row)]
    tag_w, tag_h = X(64), Y(42)
    strike = max(1, Y(2))
    for i, (t, done) in enumerate(rows):
        y0 = top + i * row
        mid = y0 + row // 2
        ink = DAILY_FADED if done else BLACK
        _owner_tag(d, x, mid - tag_h // 2, tag_w, tag_h, t.get("owner"), f_tag,
                   outline=max(1, X(4)), faded=done)
        tx = x + tag_w + X(16)
        right = x + width
        when = ""
        if done and t.get("date"):
            try:
                dd = date.fromisoformat(t["date"])
                when = f"{dd.day} {dd:%b}"
            except ValueError:
                log.warning("daily: done item with a bad date %r", t["date"])
        ww = int(d.textlength(when, font=f_txt)) if when else 0
        text = _fit(d, t.get("title") or "", f_txt, right - tx - (ww + X(16) if ww else 0))
        d.text((tx, mid), text, font=f_txt, fill=ink, anchor="lm")
        if when:
            d.text((right, mid), when, font=f_txt, fill=ink, anchor="rm")
        if done:
            d.line([(tx, mid), (tx + d.textlength(text, font=f_txt), mid)], fill=ink, width=strike)
            if when:
                d.line([(right - ww, mid), (right, mid)], fill=ink, width=strike)


def _draw_daily_caption(img: Image.Image, animal: dict, art_rect, right: int, top: int,
                        X, Y) -> tuple[int, int, int, int]:
    """The animal's name in bold over the caption as one italic paragraph,
    right-aligned at `top`, with the dashboard's lightened patch under it
    where it lies over `art_rect` (None: nothing under it). Returns the
    caption's rectangle."""
    d = ImageDraw.Draw(img)
    f_name = _font(Y(36), bold=True)
    f_cap = _font(Y(28), style="serif_italic")
    name = animal.get("common_name", "").capitalize()
    lines = _wrap(d, " · ".join(_caption_parts(animal, name=False)), f_cap, X(400))[:4]
    lh = 36
    cap_top = top + Y(50)
    w = max([d.textlength(name, font=f_name)] + [d.textlength(ln, font=f_cap) for ln in lines])
    rect = (int(right - w), top, right, cap_top + Y(lh) * len(lines))
    if art_rect and _overlaps(rect, art_rect):
        _fade_behind(img, rect, X, Y)
    d.text((right, top), name, font=f_name, fill=BLACK, anchor="ra")
    _draw_caption_lines(d, lines, f_cap, right, cap_top, Y, {}, line_h=lh)
    return rect


def daily(width: int, height: int) -> Image.Image:
    """This week's calendar and the todo list beside the daily animal. The
    todos come from the planner's export (planner_source); the calendar is
    example data until the shared calendar feed exists."""
    import state
    cfg = state.load_config()
    today = date.today()
    return _daily(width, height, _animal(cfg), planner_source.get_planner(today), today)[0]


def _daily(width: int, height: int, animal: dict | None, planner: dict, today: date):
    """The daily view drawn from its data: (image, the Art drawn or None).
    In portrait the Art is the polaroid card's rectangle, with no
    placement or geometry: the print is shown whole and can't be moved."""
    if height > width:
        return _daily_portrait(width, height, animal, planner, today)
    img = Image.new("RGB", (width, height), WHITE)
    X, Y = _scalers(width, height, LANDSCAPE)
    box = _daily_art_box(width, height)
    # A missing image (fetch failed, unreadable file) leaves its box as paper.
    art = _draw_art(img, animal, box, X, Y, mode="daily", layout=DAILY_LAYOUT)
    caption = None
    if art:
        caption = _draw_daily_caption(img, animal, art.rect, width - X(DAILY_MARGIN),
                                      Y(DAILY_MARGIN), X, Y)
    d = ImageDraw.Draw(img)
    foot_top = _daily_footer(d, planner, height, X, Y)
    top = Y(DAILY_MARGIN) + Y(2)
    list_bottom = foot_top - Y(12)
    events, todos = planner.get("events") or [], planner.get("todos") or {}
    list_x = X(DAILY_LIST_X)
    _draw_calendar(d, events, today, X(DAILY_MARGIN) + X(4), top, list_bottom,
                   list_x - X(24), X, Y, avoid=caption)
    _draw_todos(d, todos, list_x, top, X(440), list_bottom, X, Y)
    return img, art


STALE_ROW_H = 46   # the offline marker's row, above the footer line


def _daily_footer(d: ImageDraw.ImageDraw, planner: dict, height: int, X, Y) -> int:
    """Bottom-left: "Example data" while the planner is the stub, "Example
    events" while only the calendar is, and above it the offline marker
    when the export is stale. Returns the footer's top."""
    foot_top = height - Y(8) - Y(28)
    label = ("Example data" if planner.get("example")
             else "Example events" if planner.get("example_events") else None)
    if label:
        d.text((X(DAILY_MARGIN), foot_top), label, font=_font(Y(24)), fill=BLACK)
    if planner.get("stale"):
        foot_top -= Y(STALE_ROW_H)
        _stale_marker(d, X(DAILY_MARGIN), foot_top, planner["stale"], X, Y)
    return foot_top



def _stale_marker(d: ImageDraw.ImageDraw, x: int, top: int, text: str, X, Y) -> None:
    """An outlined warning triangle with "!" and the words beside it: the
    planner export is old."""
    h = Y(34)
    w = round(h * 1.15)
    y0 = top + (Y(STALE_ROW_H) - h) // 2
    d.polygon([(x + w / 2, y0), (x + w, y0 + h), (x, y0 + h)], fill=WHITE,
              outline=BLACK, width=max(2, Y(3)))
    _centered_text(d, (x + w / 2, y0 + h * 0.62), "!", _font(round(h * 0.62), bold=True), BLACK)
    d.text((x + w + X(12), y0 + h / 2), text, font=_font(Y(24)), fill=BLACK, anchor="lm")


def _daily_portrait(width: int, height: int, animal: dict | None, planner: dict,
                    today: date):
    """Portrait daily view: the week down the whole left half, the todo
    list at the bottom right, the caption top right and the print between
    them as a polaroid on twine (DECISIONS §30)."""
    img = Image.new("RGB", (width, height), WHITE)
    X, Y = _scalers(width, height, PORTRAIT)
    d = ImageDraw.Draw(img)
    foot_top = _daily_footer(d, planner, height, X, Y)
    events, todos = planner.get("events") or [], planner.get("todos") or {}
    _draw_calendar(d, events, today, X(DAILY_MARGIN) + X(4), Y(DAILY_MARGIN) + Y(2),
                   foot_top - Y(16), X(DAILY_CAL_RIGHT), X, Y, style=CAL_TALL)
    # The list hangs from the bottom margin, as tall as its rows.
    list_bottom = height - Y(DAILY_MARGIN)
    list_top = list_bottom - len(_todo_rows(todos)) * Y(DAILY_TODO_ROW_H)
    _draw_todos(d, todos, X(DAILY_TODO_X), list_top, X(DAILY_TODO_RIGHT - DAILY_TODO_X),
                list_bottom, X, Y)
    photo = _polaroid_photo(animal)
    if photo is None:
        return img, None
    caption = _draw_daily_caption(img, animal, None, width - X(DAILY_MARGIN),
                                  Y(DAILY_MARGIN), X, Y)
    card = _draw_polaroid(img, photo, caption, list_top, X, Y)
    return img, Art(card, None, None)


def _polaroid_photo(animal: dict | None) -> Image.Image | None:
    """The print as the polaroid shows it: the plate turned by its
    rotation, standardised (_prep_plate) and cut to its subject with up to
    HERO_CONTEXT of sheet around it — whole, at its own shape. None (and a
    warning) when the plate can't be read; None for no animal."""
    if not animal:
        return None
    try:
        plate = Image.open(animal["path"]).convert("RGB")
    except (OSError, KeyError) as e:
        log.warning("daily: no print drawn, plate for %s unreadable: %s",
                    animal.get("file") or animal.get("common_name"), e)
        return None
    rot = int(animal.get("rotation") or 0) % 360
    if rot:
        plate = plate.rotate(-rot, expand=True)
    sheet, sb, _ = _prep_plate(plate)
    # HERO_CONTEXT is panel px around the subject. The photo is drawn about
    # POLAROID_PHOTO_MAX_W wide, so that many panel px are this many sheet px.
    sw, sh = sb[2] - sb[0], sb[3] - sb[1]
    pad = HERO_CONTEXT * max(sw, sh) / POLAROID_PHOTO_MAX_W
    return sheet.crop((max(0, round(sb[0] - pad)), max(0, round(sb[1] - pad)),
                       min(sheet.width, round(sb[2] + pad)),
                       min(sheet.height, round(sb[3] + pad))))


def _polaroid_size(aspect: float, room_h: float, X, Y) -> tuple[int, int]:
    """The photo's size (w, h) at `aspect` (w/h): as wide as
    POLAROID_PHOTO_MAX_W allows, shrunk uniformly so the tilted card is at
    most `room_h` tall."""
    a = math.radians(abs(POLAROID_TILT))
    sin, cos = math.sin(a), math.cos(a)
    side, ends = 2 * X(POLAROID_SIDE), Y(POLAROID_TOP) + Y(POLAROID_FOOT)
    # A tilted card's height: its width * sin + its height * cos.
    ph = (room_h - side * sin - ends * cos) / (aspect * sin + cos)
    pw = min(X(POLAROID_PHOTO_MAX_W), ph * aspect)
    pw = max(1, int(pw))
    return pw, max(1, int(pw / aspect))


def _draw_polaroid(img: Image.Image, photo: Image.Image, caption, list_top: int, X, Y):
    """The photo as a tilted polaroid with a soft shadow, hung on twine
    through punched holes: in from over the top border left of the
    `caption`, and out of the bottom hole fading into the todo list at
    `list_top`. Centred at POLAROID_CX between the caption and
    POLAROID_BOTTOM. Returns the card's rectangle (frame px)."""
    from PIL import ImageFilter
    top = caption[3] + Y(POLAROID_CLEAR)
    bottom = min(Y(POLAROID_BOTTOM), list_top - Y(POLAROID_CLEAR))
    pw, ph = _polaroid_size(photo.width / photo.height, bottom - top, X, Y)
    ss = TWINE_SS
    side, top_b, foot = X(POLAROID_SIDE), Y(POLAROID_TOP), Y(POLAROID_FOOT)
    cw, ch = pw + 2 * side, top_b + ph + foot

    # The card, upright and supersampled, then turned.
    card = Image.new("RGBA", (cw * ss, ch * ss), (0, 0, 0, 0))
    cd = ImageDraw.Draw(card)
    cd.rounded_rectangle([0, 0, cw * ss - 1, ch * ss - 1], radius=3 * ss, fill=WHITE,
                         outline=POLAROID_INK, width=2 * ss)
    card.paste(photo.resize((pw * ss, ph * ss), Image.LANCZOS), (side * ss, top_b * ss))
    edge = Image.new("RGBA", card.size, (0, 0, 0, 0))   # the photo's own faint edge
    ImageDraw.Draw(edge).rectangle([side * ss, top_b * ss, (side + pw) * ss - 1,
                                    (top_b + ph) * ss - 1], outline=(0, 0, 0, 77),
                                   width=round(1.5 * ss))
    card = Image.alpha_composite(card, edge)
    cd = ImageDraw.Draw(card)
    holes = [(cw / 2, Y(POLAROID_HOLE_IN)), (cw / 2, ch - Y(POLAROID_HOLE_IN))]
    r = POLAROID_HOLE_R * ss
    for hx, hy in holes:
        cd.ellipse([hx * ss - r, hy * ss - r, hx * ss + r, hy * ss + r],
                   fill=POLAROID_HOLE, outline=POLAROID_INK, width=round(1.6 * ss))
    # PIL turns anticlockwise for a positive angle; the tilt is clockwise-positive.
    card = card.rotate(-POLAROID_TILT, resample=Image.BICUBIC, expand=True)
    card = card.resize((round(card.width / ss), round(card.height / ss)), Image.LANCZOS)
    cx, cy = X(POLAROID_CX), (top + bottom) / 2
    x0, y0 = round(cx - card.width / 2), round(cy - card.height / 2)

    (sx, sy), blur, opacity = POLAROID_SHADOW
    alpha = card.getchannel("A")
    reach = blur * 3
    shadow = Image.new("L", (card.width + 2 * reach, card.height + 2 * reach), 0)
    shadow.paste(alpha.point(lambda v: int(v * opacity)), (reach, reach))
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur / 2))
    img.paste(BLACK, (x0 - reach + X(sx), y0 - reach + Y(sy)), shadow)
    img.paste(card.convert("RGB"), (x0, y0), alpha)

    # Where the holes ended up after the turn.
    t = math.radians(POLAROID_TILT)

    def turned(px, py):
        lx, ly = px - cw / 2, py - ch / 2
        return (cx + lx * math.cos(t) - ly * math.sin(t),
                cy + lx * math.sin(t) + ly * math.cos(t))
    hole_top, hole_bottom = turned(*holes[0]), turned(*holes[1])
    rect = (x0, y0, x0 + card.width, y0 + card.height)
    _draw_twine(img, hole_top, hole_bottom, rect[3], list_top, X, Y)
    return rect


def _quad(p0, c, p1, step: float) -> list[tuple[float, float]]:
    """Points along a quadratic Bézier about `step` px apart."""
    n = max(8, int(math.dist(p0, p1) / step))
    return [((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * c[0] + t * t * p1[0],
             (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * c[1] + t * t * p1[1])
            for t in (i / n for i in range(n + 1))]


def _bowed(p0, p1, bow: float, step: float):
    """A quadratic from p0 to p1 bowed sideways by `bow` px at its middle."""
    length = math.dist(p0, p1) or 1
    nx, ny = -(p1[1] - p0[1]) / length, (p1[0] - p0[0]) / length
    c = ((p0[0] + p1[0]) / 2 + nx * bow, (p0[1] + p1[1]) / 2 + ny * bow)
    return _quad(p0, c, p1, step)


def _rope(d: ImageDraw.ImageDraw, pts, s: float) -> None:
    """Twine along `pts` at scale `s`: a faint shadow, a dark outline, a
    lighter core and short diagonal ply ticks every 6 px."""
    def line(points, fill, w):
        d.line(points, fill=fill, width=max(1, round(w * s)), joint="curve")
    line([(x + 2 * s, y + 3 * s) for x, y in pts], (0, 0, 0, 36), 6)
    line(pts, TWINE_OUTLINE + (255,), 6.5)
    line(pts, TWINE_CORE + (255,), 4)
    acc = 0.0
    for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
        seg = math.dist((xa, ya), (xb, yb))
        acc += seg
        if acc < 6 * s or not seg:
            continue
        acc = 0.0
        tx, ty = (xb - xa) / seg, (yb - ya) / seg
        nx, ny = -ty, tx
        d.line([(xb - (nx * 1.8 + tx * 1.6) * s, yb - (ny * 1.8 + ty * 1.6) * s),
                (xb + (nx * 1.8 + tx * 1.6) * s, yb + (ny * 1.8 + ty * 1.6) * s)],
               fill=TWINE_PLY + (255,), width=max(1, round(1.4 * s)))


def _knot(d: ImageDraw.ImageDraw, x: float, y: float, s: float) -> None:
    """A small knot at a hole: two short loose ends and the knot itself."""
    for a in (-0.6, 0.5):
        end = (x + math.sin(a) * 12 * s,
               y + (math.cos(a) * 10 - 14 * (1 if a < 0 else -0.2)) * s)
        d.line([(x, y), end], fill=TWINE_OUTLINE + (255,), width=round(5 * s))
        d.line([(x, y), end], fill=TWINE_CORE + (255,), width=round(2.6 * s))
    d.ellipse([x - 6 * s, y - 5 * s, x + 6 * s, y + 5 * s], fill=TWINE_CORE + (255,),
              outline=TWINE_OUTLINE + (255,), width=round(2 * s))
    d.line([(x - 3 * s, y - 2 * s), (x + 2 * s, y + 3 * s)], fill=TWINE_PLY + (255,),
           width=max(1, round(1.2 * s)))


def _draw_twine(img: Image.Image, hole_top, hole_bottom, card_bottom: int,
                list_top: int, X, Y) -> None:
    """The twine on its own layer, composited over the card: in from above
    the top border, curving into the top hole, and out of the bottom hole
    down towards the todo list, fading out between the card and the list's
    top."""
    from PIL import ImageChops
    ss = TWINE_SS
    # The layer covers the right half: the twine never reaches further left.
    lx0 = img.width // 2
    layer = Image.new("RGBA", ((img.width - lx0) * ss, img.height * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    def at(p):
        return ((p[0] - lx0) * ss, p[1] * ss)
    # Down at TWINE_TOP_X, well left of the caption (at most X(400) wide,
    # right-aligned), turning towards the hole only below it: the card's
    # top is POLAROID_CLEAR under the caption.
    top_run = _quad((X(TWINE_TOP_X), -Y(12)), (X(TWINE_TOP_X), hole_top[1]), hole_top, 3)
    tail = _bowed(hole_bottom, (X(POLAROID_CX + 4), list_top + Y(30)), 16, 3)
    for run in (top_run, tail):
        _rope(d, [at(p) for p in run], ss)
    _knot(d, *at((hole_top[0], hole_top[1] - 1)), ss)
    _knot(d, *at((hole_bottom[0], hole_bottom[1] + 1)), ss)
    layer = layer.resize((img.width - lx0, img.height), Image.LANCZOS)

    # Fade the tail out between just below the card and the list's top.
    f0, f1 = card_bottom + Y(10), list_top
    ramp = Image.new("L", (1, img.height))
    ramp.putdata([255 if y <= f0 else 0 if y >= f1 else round(255 * (f1 - y) / (f1 - f0))
                  for y in range(img.height)])
    layer.putalpha(ImageChops.multiply(layer.getchannel("A"), ramp.resize(layer.size)))
    region = img.crop((lx0, 0, img.width, img.height)).convert("RGBA")
    img.paste(Image.alpha_composite(region, layer).convert("RGB"), (lx0, 0))


# ── bingo ─────────────────────────────────────────────────────────────────────
# One of the planner's 4x4 bingo cards, the one its `bingo_card` names
# (DECISIONS §31). A done square gets a "dobber": a stippled disc with a
# solid rim in one ink, the summary still legible over it.

BINGO_INK = RED
BINGO_RULE = 2             # the grid's hairlines, layout px
BINGO_HEADER_H = 96        # title row, above the rule under it
BINGO_PAD = 24             # inside a cell, around its summary
BINGO_TEXT_PX = (46, 20)   # a summary's largest and smallest type
BINGO_LINE_H = 1.35        # a summary's line height, as a share of its type size
BINGO_DOBBER_R = 0.44      # the disc's radius, as a share of the cell's shorter side
# A done square's summary stays inside its disc: its block's corners lie
# within this share of the radius (clear of the rim and the halo).
BINGO_TEXT_IN_DISC = 0.85
BINGO_RIM = 6              # layout px
# The stipple: 2x2 dots of ink on a grid of this pitch (px), every other dot
# row offset, about a fifth of the disc. Pure ink, so Floyd-Steinberg leaves
# it as drawn (no special dither needed).
BINGO_STIPPLE_PITCH = 3


def _stipple(size: tuple[int, int]) -> Image.Image:
    """A frame-sized 'L' mask of the stipple dots (255 = ink), fixed to the
    frame's pixel grid so every disc shares one regular pattern."""
    n = BINGO_STIPPLE_PITCH
    tile = Image.new("L", (2 * n, 2 * n), 0)
    td = ImageDraw.Draw(tile)
    td.rectangle([0, 0, 1, 1], fill=255)
    td.rectangle([n, n, n + 1, n + 1], fill=255)
    out = Image.new("L", size, 0)
    for ty in range(0, size[1], 2 * n):
        for tx in range(0, size[0], 2 * n):
            out.paste(tile, (tx, ty))
    return out


def _dobber(img: Image.Image, stipple: Image.Image, cx: float, cy: float, r: float,
            rim: int) -> None:
    """The bingo dobber: a stippled disc and a solid rim, in BINGO_INK."""
    disc = Image.new("L", img.size, 0)
    ImageDraw.Draw(disc).ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    dots = Image.new("L", img.size, 0)
    dots.paste(stipple, (0, 0), disc)
    img.paste(BINGO_INK, (0, 0), dots)
    ImageDraw.Draw(img).ellipse([cx - r, cy - r, cx + r, cy + r], outline=BINGO_INK,
                                width=rim)


def _fit_summary(d: ImageDraw.ImageDraw, text: str, max_w: int, max_h: int, Y,
                 disc_r: float | None = None) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont,
                                                       int, list[str]]:
    """The largest Michroma size at which `text` wraps into the box (and,
    given `disc_r`, into a circle that radius: the wider the block is
    allowed, the shorter it must be), and its lines (at most three). At the
    smallest size, lines are cut to fit. In a disc a two-word summary takes
    a line per word."""
    big, small = BINGO_TEXT_PX
    words = text.split()
    one_per_line = disc_r is not None and len(words) == 2
    for size in range(Y(big), Y(small) - 1, -2):
        f = _font(size, style="display")
        for n in ((2, 3) if one_per_line else (1, 2, 3)):
            h = n * size * BINGO_LINE_H
            w = max_w
            if disc_r is not None:
                half = (disc_r * BINGO_TEXT_IN_DISC) ** 2 - (h / 2) ** 2
                if half <= 0:
                    break
                w = min(w, int(2 * math.sqrt(half)))
            lines = words if one_per_line and n == 2 else _wrap(d, text, f, w)
            if (len(lines) <= n and h <= max_h
                    and all(d.textlength(ln, font=f) <= w for ln in lines)):
                return f, size, lines
    f = _font(Y(small), style="display")
    return f, Y(small), [_fit(d, ln, f, max_w) for ln in _wrap(d, text, f, max_w)[:3]]


def bingo(width: int, height: int) -> Image.Image:
    """The planner's chosen bingo card (planner_source.get_bingo; the stub's
    example card, labelled, without an export)."""
    return _bingo(width, height, planner_source.get_bingo(date.today()))


def _bingo(width: int, height: int, card: dict) -> Image.Image:
    img = Image.new("RGB", (width, height), WHITE)
    portrait = height > width
    X, Y = _scalers(width, height, PORTRAIT if portrait else LANDSCAPE)
    d = ImageDraw.Draw(img)
    m = X(DAILY_MARGIN)
    foot_top = _daily_footer(d, card, height, X, Y)

    # Header: the card's title and its owner's tag left, the count right.
    f_title = _font(Y(52), style="display")
    top = Y(DAILY_MARGIN)
    mid = top + Y(BINGO_HEADER_H) // 2 - Y(8)
    d.text((m, mid), card["title"], font=f_title, fill=BLACK, anchor="lm")
    tag_x = m + int(d.textlength(card["title"], font=f_title)) + X(24)
    _owner_tag(d, tag_x, mid - Y(23), X(72), Y(46), card["owner"], _font(Y(30)),
               outline=max(1, X(4)))
    squares = card["squares"]
    done = sum(1 for sq in squares if sq["done"])
    d.text((width - m, mid), f"{done} / {len(squares)}", font=_font(Y(40), style="display"),
           fill=BLACK, anchor="rm")

    # The grid, edge to edge between the margins.
    rule = max(1, Y(BINGO_RULE))
    gx0, gy0 = m, top + Y(BINGO_HEADER_H)
    gx1, gy1 = width - m, foot_top - Y(16)
    cw, ch = (gx1 - gx0) / planner_source.GRID, (gy1 - gy0) / planner_source.GRID
    stipple = _stipple(img.size)
    r = BINGO_DOBBER_R * min(cw, ch)
    for sq in squares:
        if sq["done"]:
            _dobber(img, stipple, gx0 + (sq["col"] + 0.5) * cw, gy0 + (sq["row"] + 0.5) * ch,
                    r, max(2, Y(BINGO_RIM)))
    for i in range(planner_source.GRID + 1):
        x = round(gx0 + i * cw)
        y = round(gy0 + i * ch)
        d.line([(x, gy0), (x, gy1)], fill=BLACK, width=rule)
        d.line([(gx0, y), (gx1, y)], fill=BLACK, width=rule)

    pad = X(BINGO_PAD)
    for sq in squares:
        f, size, lines = _fit_summary(d, sq["summary"], int(cw) - 2 * pad, int(ch) - 2 * pad,
                                      Y, disc_r=r if sq["done"] else None)
        cx, cy = gx0 + (sq["col"] + 0.5) * cw, gy0 + (sq["row"] + 0.5) * ch
        lh = size * BINGO_LINE_H
        halo = {"stroke_width": max(2, Y(4)), "stroke_fill": WHITE} if sq["done"] else {}
        for i, ln in enumerate(lines):
            d.text((cx, cy + (i - (len(lines) - 1) / 2) * lh), ln, font=f, fill=BLACK,
                   anchor="mm", **halo)
    return img


# ── planner ───────────────────────────────────────────────────────────────────
# The pane's "Planner" choice: whichever planner screen the export's `view`
# asks for (daily without one).

def drawn_screen(screen: str | None) -> str | None:
    """The screen actually drawn for a configured one: "planner" becomes the
    export's view, anything else is itself."""
    return planner_source.view() if screen == "planner" else screen


def planner(width: int, height: int) -> Image.Image:
    return SCREENS[drawn_screen("planner")](width, height)


def placeholder(width: int, height: int) -> Image.Image:
    """Default screen until real dashboard content exists."""
    img = Image.new("RGB", (width, height), (255, 255, 255))
    d = ImageDraw.Draw(img)

    # Header band
    d.rectangle([0, 0, width, 140], fill=(0, 0, 0))
    d.text((48, 38), "inkplate_dash", font=_font(64, bold=True), fill=(255, 255, 255))
    now = datetime.now()
    stamp = now.strftime("%A %d %B %Y  ·  %H:%M")
    right_font = _font(40)
    left, top, right, bottom = d.textbbox((0, 0), stamp, font=right_font)
    d.text((width - (right - left) - 48, 48), stamp, font=right_font, fill=(255, 255, 0))

    _centered_text(
        d, (width / 2, height / 2 - 60),
        "Scaffolding online",
        _font(96, bold=True), (0, 0, 0),
    )
    _centered_text(
        d, (width / 2, height / 2 + 60),
        "Rendered by the HTPC · waiting for real dashboard content",
        _font(42), (255, 0, 0),
    )

    # Color chips along the bottom so even the placeholder exercises all six inks
    chip_w = width // len(SPECTRA_COLORS)
    for i, (name, rgb) in enumerate(SPECTRA_COLORS):
        x0 = i * chip_w
        d.rectangle([x0, height - 120, x0 + chip_w, height], fill=rgb)
        label_fill = (255, 255, 255) if name in ("black", "blue", "red") else (0, 0, 0)
        _centered_text(d, (x0 + chip_w / 2, height - 60), name, _font(36, bold=True), label_fill)
    return img


def palette_test(width: int, height: int) -> Image.Image:
    """Six solid horizontal bands — verifies each ink renders as expected."""
    img = Image.new("RGB", (width, height), (255, 255, 255))
    d = ImageDraw.Draw(img)
    band_h = height // len(SPECTRA_COLORS)
    for i, (name, rgb) in enumerate(SPECTRA_COLORS):
        y0 = i * band_h
        d.rectangle([0, y0, width, y0 + band_h], fill=rgb)
        label_fill = (255, 255, 255) if name in ("black", "blue", "red") else (0, 0, 0)
        d.text((48, y0 + band_h // 2 - 30), f"{i}: {name} {rgb}", font=_font(48, bold=True), fill=label_fill)
    return img


def dither_test(width: int, height: int) -> Image.Image:
    """Grayscale + hue gradients: shows what Floyd-Steinberg does with only six inks."""
    img = Image.new("RGB", (width, height), (255, 255, 255))
    d = ImageDraw.Draw(img)
    half = height // 2

    # Top half: horizontal grayscale ramp
    for x in range(width):
        v = int(255 * x / (width - 1))
        d.line([(x, 0), (x, half)], fill=(v, v, v))

    # Bottom half: hue sweep at full saturation
    import colorsys
    for x in range(width):
        r, g, b = colorsys.hsv_to_rgb(x / (width - 1), 1.0, 1.0)
        d.line([(x, half), (x, height)], fill=(int(r * 255), int(g * 255), int(b * 255)))

    d.text((48, 40), "grayscale ramp", font=_font(44, bold=True), fill=(255, 0, 0))
    d.text((48, half + 40), "hue sweep", font=_font(44, bold=True), fill=(0, 0, 0))
    return img


def alignment(width: int, height: int) -> Image.Image:
    """1px border, center crosshair, labeled corners — catches crop/offset bugs."""
    img = Image.new("RGB", (width, height), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width - 1, height - 1], outline=(0, 0, 0), width=1)
    d.rectangle([10, 10, width - 11, height - 11], outline=(255, 0, 0), width=2)
    cx, cy = width // 2, height // 2
    d.line([(cx - 100, cy), (cx + 100, cy)], fill=(0, 0, 255), width=3)
    d.line([(cx, cy - 100), (cx, cy + 100)], fill=(0, 0, 255), width=3)
    f = _font(40, bold=True)
    d.text((24, 24), "(0,0)", font=f, fill=(0, 0, 0))
    _centered_text(d, (cx, cy - 160), f"{width} x {height}", _font(56, bold=True), (0, 0, 0))
    lbl = f"({width - 1},{height - 1})"
    left, top, right, bottom = d.textbbox((0, 0), lbl, font=f)
    d.text((width - (right - left) - 24, height - (bottom - top) - 34), lbl, font=f, fill=(0, 0, 0))
    return img


SCREENS = {
    "dashboard": dashboard,
    "daily": daily,
    "planner": planner,
    "bingo": bingo,
    "placeholder": placeholder,
    "palette_test": palette_test,
    "dither_test": dither_test,
    "alignment": alignment,
}

DEFAULT_SCREEN = "dashboard"
