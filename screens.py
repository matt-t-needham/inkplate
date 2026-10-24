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
- placeholder     — proves the whole pipeline end to end
- palette_test    — six solid color bands; verifies color mapping on the panel
- dither_test     — gradients; judges dither quality
- alignment       — border, crosshairs, corner labels; catches offset/rotation
"""

from datetime import date, datetime

from PIL import Image, ImageDraw, ImageFont

import config
import datasources
from palette import SPECTRA_COLORS

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
# A contained figure is scaled past its fit by this much and the overflow
# cropped around its ink centroid: the subject box keeps a margin of paper
# and a fit-to-box plate still floats in white, so lean in. The overflow is
# at most 1 - 1/ART_ZOOM of the long axis, which the edge fades swallow.
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


def _edge_fade_mask(w: int, h: int, left: float = .2, right: float = .2,
                    top: float = .2, bottom: float = .2) -> Image.Image:
    """Alpha mask: opaque interior, constant (linear) fade to 0 across the
    given fraction of each edge. A fraction of 0 leaves that edge hard, so the
    artwork can run flush to the panel border. Built from two 1px gradient
    strips resized up, so it stays cheap at panel size."""
    from PIL import ImageChops

    def ramp(n: int, near: float, far: float) -> list[int]:
        a, b = max(1, int(n * near)), max(1, int(n * far))
        out = []
        for i in range(n):
            v = 255
            if near > 0 and i < a:
                v = min(v, int(255 * i / a))
            if far > 0 and i > n - 1 - b:
                v = min(v, int(255 * (n - 1 - i) / b))
            out.append(v)
        return out

    hx = Image.new("L", (w, 1))
    hx.putdata(ramp(w, left, right))
    vy = Image.new("L", (1, h))
    vy.putdata(ramp(h, top, bottom))
    return ImageChops.darker(hx.resize((w, h)), vy.resize((w, h)))


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

WEATHER_BLOCK_H = 440      # top of the glyph row to the bottom of the °F line
# Portrait: the weather's first row (glyph · 21° · HIGH 28°) reaches x≈920 of
# 1200, so the caption can't share that row as it does in landscape — it
# tucks under the HIGH temperature instead, right-aligned.
PORTRAIT_CAPTION_TOP = 196
# Hero: a centred composition. The weather band (_draw_weather_centred,
# HERO_WEATHER_H tall) sits on the snow row (_snow_foot, HERO_SNOW_H tall)
# or, with snow off, on the bottom margin; everything above, inside
# HERO_MARGIN, is the art section. Centred art has no edge to bleed off, so
# it gets only a soft HERO_FADE on every side — enough to hide the crop
# line, not a dissolve. The subject crop is tight (a beak or a tail tip can
# sit on it), so wherever the box has room the plate is drawn HERO_CONTEXT
# further out and the fade lands on that paper margin; only where the box
# itself stops it — the frame margin, the weather band — does the fade
# fall on the drawing.
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


def _with_rotation(animal: dict | None, rotations: dict) -> dict | None:
    """The animal dict plus its per-image "rotation" (clockwise degrees)
    from config, keyed by the Commons file — for plates scanned sideways.
    The cached dict is left untouched."""
    if not animal:
        return animal
    return {**animal, "rotation": int(rotations.get(animal.get("file"), 0)) % 360}


def _dashboard_data(cfg: dict) -> dict:
    """Every source the dashboard draws from, each independently optional."""
    return {
        "weather": datasources.get_weather(cfg["latitude"], cfg["longitude"]),
        "animal": _with_rotation(
            datasources.get_animal(offset=cfg["animal_offset"],
                                   period_days=cfg["animal_period_days"],
                                   vetoed=cfg["vetoed_images"]),
            cfg.get("image_rotations") or {}),
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
    data = _dashboard_data(cfg)
    if height > width:
        return _dashboard_portrait(width, height, cfg, data,
                                   layout=cfg.get("portrait_layout"))
    return _dashboard_landscape(width, height, cfg, data)


def _scalers(width: int, height: int, base: tuple[int, int]):
    sx, sy = width / base[0], height / base[1]
    return (lambda v: int(v * sx)), (lambda v: int(v * sy))


def _dashboard_landscape(width: int, height: int, cfg: dict, data: dict) -> Image.Image:
    img = Image.new("RGB", (width, height), WHITE)
    X, Y = _scalers(width, height, LANDSCAPE)
    halo = dict(stroke_width=max(2, Y(4)), stroke_fill=WHITE)   # text over art

    # Fixed art box in panel coordinates (see _draw_art). The caption clears
    # the top; the weather only occupies the upper left, so the box reaches
    # under it — a wide plate's left edge is inside the fade by then — and
    # runs to the bottom border.
    box = (X(440), Y(140), width, height)
    drew_art = _draw_art(img, data["animal"], box, X, Y)

    d = ImageDraw.Draw(img)
    _draw_caption(d, data["animal"] if drew_art else None, width - X(48), Y(36),
                  X, Y, halo)
    _draw_weather(d, data["weather"], X(52), Y(44), X, Y, halo)

    bottom = height - Y(52)
    if data["snow"]:
        _snow_report(d, X(52), bottom, data["snow"], X, Y, halo,
                     name=cfg["snow_location_name"])
    return img


def _dashboard_portrait(width: int, height: int, cfg: dict, data: dict,
                        layout: str | None = None) -> Image.Image:
    layout = layout if layout in PORTRAIT_LAYOUTS else DEFAULT_PORTRAIT_LAYOUT
    img = Image.new("RGB", (width, height), WHITE)
    X, Y = _scalers(width, height, PORTRAIT)
    halo = dict(stroke_width=max(2, Y(4)), stroke_fill=WHITE)
    LX = X(52)
    right = width - X(48)

    if layout == "hero":
        # Build upwards from the bottom border: snow row (optional), weather
        # band, then the art takes whatever is left above.
        bottom = height - Y(52)
        snow_top = bottom - Y(HERO_SNOW_H) if data["snow"] else bottom
        band_top = (snow_top - Y(28) if data["snow"] else bottom) - Y(HERO_WEATHER_H)
        m = X(HERO_MARGIN)
        box = (m, Y(HERO_MARGIN), width - m, band_top - Y(24))
        art_rect = _draw_art(img, data["animal"], box, X, Y, mode="centered")
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
        return img

    # stack: the landscape arrangement folded — info on top, art below.
    drew_art = _draw_art(img, data["animal"], (0, Y(500), width, height), X, Y)
    d = ImageDraw.Draw(img)
    _draw_caption(d, data["animal"] if drew_art else None, right,
                  Y(PORTRAIT_CAPTION_TOP), X, Y, halo)
    _draw_weather(d, data["weather"], LX, Y(44), X, Y, halo)

    bottom = height - Y(52)
    if data["snow"]:
        _snow_report(d, LX, bottom, data["snow"], X, Y, halo,
                     name=cfg["snow_location_name"])
    return img


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


def _draw_art(img: Image.Image, animal: dict | None, box, X, Y,
              mode: str = "bleed"):
    """The engraving, fitted into a fixed art box (x0, y0, x1, y1) in frame
    pixels. The box is defined in panel coordinates, not derived from the
    image, so every plate lands in the same place whatever its dimensions.
    A figure is contained, then leaned into by ART_ZOOM (the overflow is
    paper margin, cut around the ink centroid); a scene covers the box.
    `animal["rotation"]` (clockwise degrees, from config) turns a plate
    that was scanned sideways before anything else looks at it.

    mode "bleed" (landscape, stack): anchored to the box's right edge with
    the last ART_BLEED running off the frame, long fades on the left and
    the top/bottom edges so text can sit over it.
    mode "centered" (hero): the plate's ink centroid is put on the box's
    centre (clamped so nothing leaves the box) and drawn with up to
    HERO_CONTEXT of paper around it, so the soft HERO_FADE falls on that
    margin rather than the subject wherever the box has room — nothing
    overlaps it except its own caption.
    Returns the pasted rectangle, or None if nothing was drawn."""
    if not animal:
        return None
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
    fit = max if is_scene else min
    scale = fit(bw / art.width, bh / art.height)
    if not is_scene:
        scale *= ART_ZOOM
    art = art.resize((max(1, round(art.width * scale)),
                      max(1, round(art.height * scale))), Image.LANCZOS)
    if art.width > bw or art.height > bh:
        # Trim the overflow: a scene around its centre, a zoomed figure
        # around its ink centroid so the body stays and the margin goes.
        if is_scene:
            cx, cy = art.width / 2, art.height / 2
        else:
            cx, cy = _ink_centroid(art)
        ox = max(0, min(art.width - bw, round(cx - bw / 2)))
        oy = max(0, min(art.height - bh, round(cy - bh / 2)))
        art = art.crop((ox, oy, ox + min(bw, art.width), oy + min(bh, art.height)))
    else:
        ox = oy = 0
    # Fade widths are fixed on the panel, not a share of the image —
    # otherwise a small plate fades over a few pixels and a large one
    # over hundreds, which is what made the peacock dissolve early.
    if mode == "centered":
        cx, cy = _ink_centroid(art)
        x = round(bx0 + bw / 2 - cx)
        y = round(by0 + bh / 2 - cy)
        x = max(bx0, min(bx1 - art.width, x))
        y = max(by0, min(by1 - art.height, y))
        art, x, y = _with_context(sheet, sb, scale, (x - ox, y - oy), box, X, Y)
        fx, fy = min(0.45, X(HERO_FADE) / art.width), min(0.45, Y(HERO_FADE) / art.height)
        mask = _edge_fade_mask(art.width, art.height, left=fx, right=fx, top=fy, bottom=fy)
    else:
        edge = min(0.45, Y(ART_FADE_EDGE) / art.height)
        mask = _edge_fade_mask(
            art.width, art.height,
            left=min(0.45, X(ART_FADE_LEFT) / art.width), right=0.0,
            top=edge, bottom=edge)
        x = bx1 - round(art.width * (1 - ART_BLEED))
        y = by0 + (bh - art.height) // 2
    img.paste(art, (x, y), mask)
    return (x, y, x + art.width, y + art.height)


def _with_context(sheet: Image.Image, sb, scale: float, origin, box, X, Y):
    """The subject plus up to HERO_CONTEXT of the sheet around it, clipped to
    the art box. `origin` is where the scaled subject box's top-left lands in
    the frame. Returns (image, x, y)."""
    ex, ey = X(HERO_CONTEXT) / scale, Y(HERO_CONTEXT) / scale   # in sheet px
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


def _caption_lines(d: ImageDraw.ImageDraw, animal: dict, font, max_w: int,
                   max_lines: int = 4) -> list[str]:
    """Source caption lines: "Rhinoceros · Rhinoceros unicornis /
    of South Asia / as depicted by Conrad Gessner, Zürich, 1551". One idea
    per line, each wrapped at max_w."""
    line1 = animal.get("common_name", "?").capitalize()
    if animal.get("sci_name"):
        line1 += f" · {animal['sci_name']}"
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
    lines = []
    for part in [line1] + [r for r in rest if r]:
        lines += _wrap(d, part, font, max_w)
    return lines[:max_lines]


def _draw_caption_lines(d: ImageDraw.ImageDraw, lines: list[str], font, right: int,
                        top: int, Y, halo) -> None:
    cy = top
    for ln in lines:
        d.text((right - d.textlength(ln, font=font), cy), ln, font=font,
               fill=BLACK, **halo)
        cy += Y(34)


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
    "placeholder": placeholder,
    "palette_test": palette_test,
    "dither_test": dither_test,
    "alignment": alignment,
}

DEFAULT_SCREEN = "dashboard"
