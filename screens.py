"""Screen registry — each screen is a function that draws a 1600x1200 RGB frame.

Extension point: write a function that takes (width, height) and returns a PIL
RGB image, add it to SCREENS, and it appears in the control pane's picker.

Screens:
- dashboard       — the real thing: weather, daily talking point, daily
                    1800s animal engraving, optional rage.bix now-playing
- placeholder     — proves the whole pipeline end to end
- palette_test    — six solid color bands; verifies color mapping on the panel
- dither_test     — gradients; judges dither quality
- alignment       — border, crosshairs, corner labels; catches offset/rotation
"""

from datetime import date, datetime

from PIL import Image, ImageDraw, ImageFont

import config
import datasources
import questions
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


def dashboard(width: int, height: int) -> Image.Image:
    """Minimal dashboard. The daily engraving is a soft, edge-faded background
    bleeding off the bottom-right corner; weather sits top-left; the talking
    point runs along the bottom border; source caption top-right. Display
    type is Michroma (the rage channel font), the question is serif italic.
    Every widget degrades independently — a dead API never blanks the panel."""
    # Lazy import: state imports screens (for DEFAULT_SCREEN), so importing
    # state at module top would be circular. At call time it's fully loaded.
    import state
    cfg = state.load_config()

    weather = datasources.get_weather(cfg["latitude"], cfg["longitude"])
    animal = datasources.get_animal(offset=cfg["animal_offset"],
                                    period_days=cfg["animal_period_days"])
    now_playing = datasources.get_now_playing() if cfg["show_now_playing"] else None
    q = questions.question_for(date.today(), cfg["question_offset"],
                               cfg["question_period_days"])

    img = Image.new("RGB", (width, height), WHITE)
    sx = width / 1600  # layout is 1600x1200-based; scales for small test renders
    sy = height / 1200

    def X(v):
        return int(v * sx)

    def Y(v):
        return int(v * sy)

    # White halo behind text that sits over the artwork.
    halo = dict(stroke_width=max(2, Y(4)), stroke_fill=WHITE)

    # ── background: the engraving, fitted into a fixed art box ───────────────
    # The box is defined in panel coordinates, not derived from the image, so
    # every plate lands in the same place whatever its dimensions. Its top and
    # bottom are safe areas: the caption sits above it, the talking point
    # below. A figure is contained (never cut), a scene covers the box; either
    # way the last ART_BLEED of the artwork runs off the right border.
    # Nearly the full height of the right-hand side: the caption clears the
    # top, and the talking point only occupies the lower *left*, so the art
    # may run past it — its left edge is inside the fade by then.
    bx0, by0 = X(560), Y(150)
    bw, bh = X(1600) - bx0, Y(1150) - by0

    drew_art = False
    if animal:
        try:
            art, is_scene = _prep_engraving(Image.open(animal["path"]).convert("RGB"))
            fit = max if is_scene else min
            scale = fit(bw / art.width, bh / art.height)
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
            mask = _edge_fade_mask(
                art.width, art.height,
                left=min(0.45, X(ART_FADE_LEFT) / art.width), right=0.0,
                top=min(0.45, Y(ART_FADE_EDGE) / art.height),
                bottom=min(0.45, Y(ART_FADE_EDGE) / art.height))
            img.paste(art, (width - round(art.width * (1 - ART_BLEED)),
                            by0 + (bh - art.height) // 2), mask)
            drew_art = True
        except OSError:
            drew_art = False

    d = ImageDraw.Draw(img)

    # ── caption, top-right: "Rhinoceros · Rhinoceros unicornis /
    #    of South Asia · as depicted by Conrad Gessner, Zürich, 1551" ─────────
    cap_lines = []
    if drew_art:
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
        bits2 = [f"of {animal['native']}" if animal.get("native") else None, clause]
        line2 = " · ".join(b for b in bits2 if b)
        f_cap = _font(Y(26), style="serif_italic")
        cap_lines = (_wrap(d, line1, f_cap, X(700)) + _wrap(d, line2, f_cap, X(700)))[:3]
        cy = Y(36)
        for ln in cap_lines:
            d.text((width - X(48) - d.textlength(ln, font=f_cap), cy),
                   ln, font=f_cap, fill=BLACK, **halo)
            cy += Y(34)

    # ── now playing, under the caption ────────────────────────────────────────
    if now_playing and (now_playing.get("artist") or now_playing.get("song")):
        yr = f" ({now_playing['year']})" if now_playing.get("year") else ""
        txt = f"{now_playing.get('artist', '?')} — {now_playing.get('song', '?')}{yr}"
        f_np = _font(Y(24), style="display")
        while d.textlength(txt, font=f_np) > X(700) and len(txt) > 4:
            txt = txt[:-4] + "…"
        ny = Y(36) + Y(34) * len(cap_lines) + Y(12)
        tx = width - X(48) - d.textlength(txt, font=f_np)
        d.polygon([(tx - X(32), ny + Y(3)), (tx - X(32), ny + Y(23)),
                   (tx - X(13), ny + Y(13))], fill=GREEN)
        d.text((tx, ny), txt, font=f_np, fill=BLACK, **halo)

    # ── weather, top-left: current conditions and sun times are centered on
    #    the three-day row below them ───────────────────────────────────────────
    if weather:
        # Centre of the three-day row: columns sit at 170/435/700.
        col_x = [170 + i * 265 for i in range(3)]
        CX = X(col_x[1])

        cur_c = weather["current_c"]
        cur_kind = datasources.icon_kind(weather["current_code"])
        icon_r = Y(72)
        icon_w = _weather_icon_width(d, icon_r, cur_kind)
        temp_w = _deg(d, 0, 0, str(round(cur_c)), Y(140), BLACK, measure=True)
        gap = X(30)
        left = CX - (icon_w + gap + temp_w) / 2
        _weather_icon(d, int(left + icon_w / 2), Y(215), icon_r, cur_kind)
        _deg(d, int(left + icon_w + gap), Y(115), str(round(cur_c)), Y(140),
             BLACK, halo=halo)

        f_w = _deg(d, 0, 0, str(_c_to_f(cur_c)), Y(44), RED, suffix="F", measure=True)
        _deg(d, int(CX - f_w / 2), Y(344), str(_c_to_f(cur_c)), Y(44), RED,
             suffix="F", halo=halo)

        today = weather["days"][0] if weather["days"] else None
        if today:
            f_t = _font(Y(34), style="display")
            gsz, gpad, pair_gap = Y(78), X(14), X(56)
            gw = _glyph_width(d, WI_SUNRISE, gsz)
            rise, set_ = _hhmm(today["sunrise"]), _hhmm(today["sunset"])
            w_rise = d.textlength(rise, font=f_t)
            w_set = d.textlength(set_, font=f_t)
            total = (gw + gpad + w_rise) + pair_gap + (gw + gpad + w_set)
            x = CX - total / 2
            _sun_horizon(d, int(x + gw / 2), Y(480), gsz, True)
            d.text((x + gw + gpad, Y(464)), rise, font=f_t, fill=BLACK, **halo)
            x += gw + gpad + w_rise + pair_gap
            _sun_horizon(d, int(x + gw / 2), Y(480), gsz, False)
            d.text((x + gw + gpad, Y(464)), set_, font=f_t, fill=BLACK, **halo)

        for i, day in enumerate(weather["days"][:3]):
            cx = X(col_x[i])
            try:
                name = ("TODAY", "TOMORROW")[i] if i < 2 else \
                    date.fromisoformat(day["date"]).strftime("%a").upper()
            except ValueError:
                name = "?"
            # Glyph on top, then the day name, then the temperatures.
            _weather_icon(d, cx, Y(628), Y(40), datasources.icon_kind(day["code"]))
            f_day = _font(Y(30), style="display")
            d.text((cx - d.textlength(name, font=f_day) / 2, Y(714)),
                   name, font=f_day, fill=BLACK, **halo)
            # Low on the left, high on the right — everywhere, both units.
            lo, hi = str(round(day["tmin"])), str(round(day["tmax"]))
            w_lo = _deg(d, 0, 0, lo, Y(40), BLUE, measure=True)
            w_hi = _deg(d, 0, 0, hi, Y(40), RED, measure=True)
            px0 = int(cx - (w_lo + X(18) + w_hi) / 2)
            _deg(d, px0, Y(784), lo, Y(40), BLUE, halo=halo)
            _deg(d, px0 + w_lo + X(18), Y(784), hi, Y(40), RED, halo=halo)
            fr = f"{_c_to_f(day['tmin'])} / {_c_to_f(day['tmax'])} °F"
            f_fr = _font(Y(24))
            d.text((cx - d.textlength(fr, font=f_fr) / 2, Y(854)),
                   fr, font=f_fr, fill=BLACK, **halo)
    else:
        d.text((X(60), Y(200)), "weather unavailable",
               font=_font(Y(40), style="display"), fill=RED, **halo)

    # ── talking point, running along the bottom border ───────────────────────
    f_q = _font(Y(40), style="serif_italic")
    qlines = _wrap(d, q["text"], f_q, X(1060))[:4]
    line_h = Y(54)
    qy = height - Y(52) - line_h * len(qlines)
    tx0 = X(52)
    for ln in qlines:
        d.text((tx0, qy), ln, font=f_q, fill=BLACK, **halo)
        qy += line_h

    return img


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
