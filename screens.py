"""Screen registry — each screen is a function that draws an RGB frame.

Extension point: write a function that takes (width, height) and returns a PIL
RGB image, add it to SCREENS, and it appears in the control pane's picker.
Screens are size-agnostic: the renderer asks for 1600x1200 when the panel
hangs landscape and 1200x1600 when it is rotated to portrait (it turns the
finished frame back to the panel's native orientation itself).

Screens:
- dashboard       — the real thing: weather, daily 1800s animal engraving,
                    seasonal snow report, optional rage.bix now-playing and
                    daily quote; its own portrait layout when taller than wide
- placeholder     — proves the whole pipeline end to end
- palette_test    — six solid color bands; verifies color mapping on the panel
- dither_test     — gradients; judges dither quality
- alignment       — border, crosshairs, corner labels; catches offset/rotation
"""

from datetime import date, datetime

from PIL import Image, ImageDraw, ImageFont

import config
import datasources
import quotes
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


def _subject_box(img: Image.Image, pad: float = 0.06):
    mask, (sw, sh) = _ink_mask(img)
    cols, rows = _profiles(mask, (sw, sh))
    x0, x1 = _dominant_run(cols)
    y0, y1 = _dominant_run(rows)
    W, H = img.size
    sx, sy = W / sw, H / sh
    px, py = (x1 - x0) * pad * sx, (y1 - y0) * pad * sy
    return (max(0, int(x0 * sx - px)), max(0, int(y0 * sy - py)),
            min(W, int((x1 + 1) * sx + px)), min(H, int((y1 + 1) * sy + py)))


def _prep_engraving(art: Image.Image) -> tuple[Image.Image, bool]:
    """Trim, crop to the subject, clean the paper. Returns (image, is_scene)."""
    art = _trim_borders(art)

    mask, size = _ink_mask(art)
    coverage = sum(1 for v in mask.getdata() if v) / (size[0] * size[1])
    is_scene = coverage > SCENE_INK_COVERAGE

    box = _subject_box(art)
    W, H = art.size
    if (box[2] - box[0]) * (box[3] - box[1]) >= 0.02 * W * H:
        art = art.crop(box)   # guard only against a degenerate sliver

    wp = max(180, min(240, _paper_tone(art.convert("L")) - 5))
    return art.point(lambda v: 255 if v >= wp else int(v * 255 / wp)), is_scene


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
#   stack — weather top-left, caption top-right, the engraving fills the lower
#           two thirds; snow report bottom-left over the art's faded edge.
#   hero  — the engraving fills the top two thirds and bleeds off the top and
#           right; weather, caption and snow share a band along the bottom.

LANDSCAPE = (1600, 1200)   # layout coordinates; renders scale from these
PORTRAIT = (1200, 1600)

PORTRAIT_LAYOUTS = ("stack", "hero")
DEFAULT_PORTRAIT_LAYOUT = "stack"

WEATHER_BLOCK_H = 440      # top of the glyph row to the bottom of the °F line
# Portrait: the weather's first row (glyph · 21° · HIGH 28°) reaches x≈920 of
# 1200, so the caption can't share that row as it does in landscape — it
# tucks under the HIGH temperature instead, right-aligned.
PORTRAIT_CAPTION_TOP = 196
# Hero band: weather on the left; caption and (when on) the snow report
# stacked on the right under the HIGH column, which needs a taller band.
HERO_BAND_H = WEATHER_BLOCK_H
HERO_BAND_H_SNOW = 540


def _dashboard_data(cfg: dict) -> dict:
    """Every source the dashboard draws from, each independently optional."""
    return {
        "weather": datasources.get_weather(cfg["latitude"], cfg["longitude"]),
        "animal": datasources.get_animal(offset=cfg["animal_offset"],
                                         period_days=cfg["animal_period_days"],
                                         vetoed=cfg["vetoed_animals"]),
        "now_playing": datasources.get_now_playing() if cfg["show_now_playing"] else None,
        "snow": (datasources.get_snow(cfg["snow_latitude"], cfg["snow_longitude"])
                 if cfg["show_snow"] else None),
        # Off by default: the quote is kept (fetcher, vetoes, pane card) but
        # not drawn, so the engraving gets the room instead.
        "quote": (quotes.get_quote(date.today(), fits=quote_fits,
                                   vetoed=cfg["vetoed_quotes"])
                  if cfg.get("show_quote") else None),
    }


def dashboard(width: int, height: int) -> Image.Image:
    """Minimal dashboard. The daily engraving is a soft, edge-faded background
    bleeding off the right border; weather sits top-left; source caption
    top-right; the snow report (seasonal) and the quote (off by default) along
    the bottom. Display type is Michroma (the rage channel font), the caption
    and quote are serif italic. Every widget degrades independently — a dead
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
    # under it — a wide plate's left edge is inside the fade by then. With the
    # quote off the box runs to the bottom border and further left.
    if data["quote"]:
        box = (X(560), Y(150), width, Y(1150))
    else:
        box = (X(440), Y(140), width, height)
    drew_art = _draw_art(img, data["animal"], box, X, Y)

    d = ImageDraw.Draw(img)
    n = _draw_caption(d, data["animal"] if drew_art else None,
                      width - X(48), Y(36), X, Y, halo)
    _draw_now_playing(d, data["now_playing"], width - X(48),
                      Y(36) + Y(34) * n + Y(12), X, Y, halo)
    _draw_weather(d, data["weather"], X(52), Y(44), X, Y, halo)

    bottom = height - Y(52)
    if data["quote"]:
        bottom = _draw_quote(d, data["quote"], X(52), bottom, X, Y, halo) - Y(40)
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
        # Bottom band first (it sets how much room the art gets above it).
        band_bottom = height - Y(52)
        if data["quote"]:
            band_bottom = _draw_quote(ImageDraw.Draw(img), data["quote"], LX,
                                      band_bottom, X, Y, halo) - Y(40)
        band_top = band_bottom - Y(HERO_BAND_H_SNOW if data["snow"] else HERO_BAND_H)
        drew_art = _draw_art(img, data["animal"], (0, 0, width, band_top - Y(40)),
                             X, Y, valign="hero")
        d = ImageDraw.Draw(img)
        _draw_weather(d, data["weather"], LX, band_top, X, Y, halo)
        cap_top = band_top + Y(PORTRAIT_CAPTION_TOP - 44)
        n = _draw_caption(d, data["animal"] if drew_art else None, right,
                          cap_top, X, Y, halo)
        _draw_now_playing(d, data["now_playing"], right,
                          cap_top + Y(34) * n + Y(12), X, Y, halo)
        if data["snow"]:
            _snow_report(d, X(560), band_bottom, data["snow"], X, Y, halo,
                         name=cfg["snow_location_name"])
        return img

    # stack: the landscape arrangement folded — info on top, art below.
    drew_art = _draw_art(img, data["animal"], (0, Y(500), width, height), X, Y)
    d = ImageDraw.Draw(img)
    n = _draw_caption(d, data["animal"] if drew_art else None, right,
                      Y(PORTRAIT_CAPTION_TOP), X, Y, halo)
    _draw_now_playing(d, data["now_playing"], right,
                      Y(PORTRAIT_CAPTION_TOP) + Y(34) * n + Y(12), X, Y, halo)
    _draw_weather(d, data["weather"], LX, Y(44), X, Y, halo)

    bottom = height - Y(52)
    if data["quote"]:
        bottom = _draw_quote(d, data["quote"], LX, bottom, X, Y, halo) - Y(40)
    if data["snow"]:
        _snow_report(d, LX, bottom, data["snow"], X, Y, halo,
                     name=cfg["snow_location_name"])
    return img


# ── dashboard widgets ─────────────────────────────────────────────────────────

def _draw_art(img: Image.Image, animal: dict | None, box, X, Y,
              valign: str = "center") -> bool:
    """The engraving, fitted into a fixed art box (x0, y0, x1, y1) in frame
    pixels. The box is defined in panel coordinates, not derived from the
    image, so every plate lands in the same place whatever its dimensions.
    A figure is contained (never cut), a scene covers the box; either way the
    art is anchored to the box's right edge with the last ART_BLEED of it
    running off the frame. valign "center" centres it in the box; "hero"
    (the portrait hero layout) anchors a plate that fills the box's height
    to the top edge the same way, bleeding off it, and sits a wide one on the
    box's bottom edge so it meets the info band instead of floating.
    Returns True if something was drawn."""
    if not animal:
        return False
    bx0, by0, bx1, by1 = box
    bw, bh = bx1 - bx0, by1 - by0
    try:
        art, is_scene = _prep_engraving(Image.open(animal["path"]).convert("RGB"))
    except OSError:
        return False
    fit = max if is_scene else min
    scale = fit(bw / art.width, bh / art.height)
    fills_height = is_scene or bh / art.height <= bw / art.width
    bleed_top = valign == "hero" and fills_height
    art = art.resize((max(1, round(art.width * scale)),
                      max(1, round(art.height * scale))), Image.LANCZOS)
    if is_scene:   # cover: trim the overflow around the centre
        ox, oy = (art.width - bw) // 2, (art.height - bh) // 2
        art = art.crop((max(0, ox), max(0, oy),
                        max(0, ox) + min(bw, art.width),
                        max(0, oy) + min(bh, art.height)))
    # Fade widths are fixed on the panel, not a share of the image —
    # otherwise a small plate fades over a few pixels and a large one
    # over hundreds, which is what made the peacock dissolve early.
    edge = min(0.45, Y(ART_FADE_EDGE) / art.height)
    mask = _edge_fade_mask(
        art.width, art.height,
        left=min(0.45, X(ART_FADE_LEFT) / art.width), right=0.0,
        top=0.0 if bleed_top else edge, bottom=edge)
    x = bx1 - round(art.width * (1 - ART_BLEED))
    if bleed_top:
        y = by0 - round(art.height * ART_BLEED)
    elif valign == "hero":
        y = by1 - art.height
    else:
        y = by0 + (bh - art.height) // 2
    img.paste(art, (x, y), mask)
    return True


def _draw_caption(d: ImageDraw.ImageDraw, animal: dict | None, right: int, top: int,
                  X, Y, halo, max_w: int | None = None, max_lines: int = 4) -> int:
    """Right-aligned source caption: "Rhinoceros · Rhinoceros unicornis /
    of South Asia / as depicted by Conrad Gessner, Zürich, 1551". One idea
    per line, wrapped narrow enough to stay clear of the weather block's HIGH
    column. Returns the number of lines drawn."""
    if not animal:
        return 0
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
    f_cap = _font(Y(26), style="serif_italic")
    lines = []
    for part in [line1] + [r for r in rest if r]:
        lines += _wrap(d, part, f_cap, max_w or X(CAPTION_WIDTH))
    lines = lines[:max_lines]
    cy = top
    for ln in lines:
        d.text((right - d.textlength(ln, font=f_cap), cy), ln, font=f_cap,
               fill=BLACK, **halo)
        cy += Y(34)
    return len(lines)


def _draw_now_playing(d: ImageDraw.ImageDraw, now_playing: dict | None, right: int,
                      top: int, X, Y, halo) -> None:
    """"Artist — Song (Year)" behind a green play triangle, right-aligned."""
    if not (now_playing and (now_playing.get("artist") or now_playing.get("song"))):
        return
    yr = f" ({now_playing['year']})" if now_playing.get("year") else ""
    txt = f"{now_playing.get('artist', '?')} — {now_playing.get('song', '?')}{yr}"
    f_np = _font(Y(24), style="display")
    while d.textlength(txt, font=f_np) > X(700) and len(txt) > 4:
        txt = txt[:-4] + "…"
    tx = right - d.textlength(txt, font=f_np)
    d.polygon([(tx - X(32), top + Y(3)), (tx - X(32), top + Y(23)),
               (tx - X(13), top + Y(13))], fill=GREEN)
    d.text((tx, top), txt, font=f_np, fill=BLACK, **halo)


def _draw_weather(d: ImageDraw.ImageDraw, weather: dict | None, LX: int, top: int,
                  X, Y, halo) -> None:
    """Everything flush to one left edge: today's forecast glyph (what the
    day will do — the window shows what it's doing now) beside the current
    temperature and today's high; sunrise/sunset; then the next two days.
    `top` is the top of the temperature; the block is WEATHER_BLOCK_H tall."""
    def P(v):   # layout rows were drawn at absolute panel y's from 44 down
        return top + Y(v - 44)

    if not (weather and weather.get("days")):
        d.text((LX, P(100)), "weather unavailable",
               font=_font(Y(40), style="display"), fill=RED, **halo)
        return
    today = weather["days"][0]
    kind = datasources.icon_kind(today["code"])
    icon_r = Y(62)
    icon_w = _weather_icon_width(d, icon_r, kind)
    _weather_icon(d, LX + icon_w // 2, P(132), icon_r, kind)
    x = LX + icon_w + X(28)
    x += _temp_cf(d, x, P(44), weather["current_c"], Y(130), BLACK, halo) + X(46)
    f_lab = _font(Y(20), style="display")
    d.text((x, P(62)), "HIGH", font=f_lab, fill=BLACK, **halo)
    _temp_cf(d, x, P(92), today["tmax"], Y(64), RED, halo)

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


def _draw_quote(d: ImageDraw.ImageDraw, quote: dict, LX: int, bottom: int,
                X, Y, halo) -> int:
    """Quote in serif italic with the attribution line beneath, its last
    line ending at `bottom`. Returns the y of its top edge, so whatever
    stacks above it (the snow report) can sit just clear."""
    f_q = _font(Y(QUOTE_SIZE), style="serif_italic")
    f_a = _font(Y(24))
    qlines = _wrap(d, f"“{quote['text']}”", f_q, X(QUOTE_WIDTH))[:QUOTE_MAX_LINES]
    attr = quote_attribution(quote)
    if d.textlength(attr, font=f_a) > X(QUOTE_WIDTH):
        attr = quote_attribution(quote, with_work=False)
    line_h = Y(54)
    qtop = bottom - Y(36) - line_h * len(qlines)
    qy = qtop
    for ln in qlines:
        d.text((LX, qy), ln, font=f_q, fill=BLACK, **halo)
        qy += line_h
    d.text((LX, qy + Y(6)), attr, font=f_a, fill=BLACK, **halo)
    return qtop


# ── dashboard helpers: quote, temperatures, snow report ──────────────────────

CAPTION_WIDTH = 520   # top-right animal caption; wider collides with HIGH
QUOTE_SIZE = 40        # serif italic, panel px
QUOTE_WIDTH = 1060     # leaves the art its right-hand side
QUOTE_MAX_LINES = 3    # longer quotes are skipped, not truncated


def quote_fits(text: str) -> bool:
    """Would `text` wrap to QUOTE_MAX_LINES or fewer at full panel size? Always
    measured at 1600x1200 so the day's pick never depends on render size."""
    d = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    f = _font(QUOTE_SIZE, style="serif_italic")
    return len(_wrap(d, f"“{text}”", f, QUOTE_WIDTH)) <= QUOTE_MAX_LINES


def quote_attribution(q: dict, with_work: bool = True) -> str:
    """'— Mark Twain, Following the Equator (1897)'. Year is always present
    (quotes.get_quote rejects undated quotes); the work is optional."""
    who = q.get("author") or ""
    if with_work and q.get("work"):
        who += f", {q['work']}"
    return f"— {who} ({q['year']})" if q.get("year") else f"— {who}"


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


def _snow_report(d, x, bottom, snow, X, Y, halo, name: str = ""):
    """Location label, snowpack row (base, +72 h with a small red "3d" range
    tag), then a row
    of the coming days: forecast glyph, with the cm amount over the weekday
    centred on it. Heavy days (≥ SNOW_HEAVY_CM) in blue. `bottom` is the
    bottom edge, so it always sits just above the quote."""
    f_big = _font(Y(52), style="display")
    f_lab = _font(Y(19), style="display")
    f_amt = _font(Y(30), style="display")
    f_tag = _font(Y(20), style="display")
    depth, recent = _cm(snow.get("depth_cm")), _cm(snow.get("recent_cm"))

    # Glyph row (bottom): glyph | amount over weekday, tight.
    gy = bottom - Y(30)                       # glyph centre line
    x1 = x
    for dy in (snow.get("days") or [])[:3]:
        cm = _cm(dy.get("snow_cm"))
        k = datasources.icon_kind(dy.get("code"))
        iw = _weather_icon_width(d, Y(26), k)
        _weather_icon(d, x1 + iw // 2, gy, Y(26), k)
        x1 += iw + X(8)
        amt, dw = f"{cm}cm", _dow(dy.get("date")).upper()
        # amount + label stacked, the pair's centre on the glyph's centre
        h_amt = d.textbbox((0, 0), amt, font=f_amt)[3]
        h_dw = d.textbbox((0, 0), dw, font=f_lab)[3]
        gap = Y(4)
        ty = gy - (h_amt + gap + h_dw) // 2
        d.text((x1, ty), amt, font=f_amt,
               fill=BLUE if cm >= SNOW_HEAVY_CM else BLACK, **halo)
        d.text((x1, ty + h_amt + gap), dw, font=f_lab, fill=BLACK, **halo)
        x1 += max(d.textlength(amt, font=f_amt), d.textlength(dw, font=f_lab)) + X(26)

    # Snowpack row (above): big figures, a little air, then the small label.
    y2 = gy - Y(48) - Y(98)
    x2 = x
    for big, tag, lab in ((str(depth), None, f"CM BASE · {round(depth / 2.54)} IN"),
                          (f"+{recent}", "3d", f"CM · {round(recent / 2.54)} IN")):
        d.text((x2, y2), big, font=f_big, fill=BLACK, **halo)
        w = d.textlength(big, font=f_big)
        if tag:
            d.text((x2 + w + X(6), y2 + Y(4)), tag, font=f_tag, fill=RED, **halo)
            w += X(6) + d.textlength(tag, font=f_tag)
        d.text((x2, y2 + Y(66)), lab, font=f_lab, fill=BLACK, **halo)
        x2 += max(w, d.textlength(lab, font=f_lab)) + X(48)

    # Location label on top, same size as the row labels.
    if name:
        d.text((x, y2 - Y(30)), name, font=f_lab, fill=BLACK, **halo)


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
