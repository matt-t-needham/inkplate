"""Spectra-6 palette definitions and dithering.

The panel can physically show exactly six colors. The server pre-dithers every
frame to those six colors so the firmware never has to process pixels — it
receives an image that is already legal for the panel.

Two palettes exist on purpose:

- DITHER_PALETTE: the saturated ideal colors the firmware maps to panel color
  indexes (this is what e-paper vendors' own tooling dithers against).
- PANEL_SIM_PALETTE: muted approximations of what Spectra pigment actually
  looks like on paper — used only by the control-pane "panel simulation"
  preview so Matt sees something closer to reality than neon RGB.
"""

from PIL import Image

# Order matters: the index in this list is the color index the firmware uses.
# Keep in sync with firmware/inkplate_dash/inkplate_dash.ino (PALETTE comment).
SPECTRA_COLORS = [
    ("black", (0, 0, 0)),
    ("white", (255, 255, 255)),
    ("yellow", (255, 255, 0)),
    ("red", (255, 0, 0)),
    ("blue", (0, 0, 255)),
    ("green", (0, 255, 0)),
]

DITHER_PALETTE = [rgb for _, rgb in SPECTRA_COLORS]

# Measured-ish pigment tones (e-paper "white" is newsprint grey, colors are
# desaturated). Purely cosmetic; never sent to the device.
PANEL_SIM_PALETTE = [
    (40, 40, 40),      # black
    (225, 222, 210),   # white (paper)
    (208, 190, 71),    # yellow
    (156, 72, 65),     # red
    (75, 92, 145),     # blue
    (88, 128, 90),     # green
]


def _palette_image() -> Image.Image:
    """A 1px 'P' image carrying the 6-color palette, for Image.quantize()."""
    flat = []
    for rgb in DITHER_PALETTE:
        flat.extend(rgb)
    # Pillow requires a 256-entry palette; pad by repeating the last color so
    # unused slots can never win a nearest-color comparison unexpectedly.
    flat.extend(DITHER_PALETTE[-1] * (256 - len(DITHER_PALETTE)))
    pal_img = Image.new("P", (1, 1))
    pal_img.putpalette(flat)
    return pal_img


_PAL_IMG = _palette_image()


def dither_to_spectra(img: Image.Image) -> Image.Image:
    """Floyd-Steinberg dither an RGB image down to the 6 Spectra colors.

    Runs in Pillow's C core (~100ms for 1600x1200), returns a 'P' image whose
    palette is exactly DITHER_PALETTE — saved as PNG it stays indexed, small,
    and trivially mappable to panel color indexes on the device.
    """
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img.quantize(palette=_PAL_IMG, dither=Image.Dither.FLOYDSTEINBERG)


def simulate_panel(dithered: Image.Image) -> Image.Image:
    """Remap a dithered 'P' image to muted pigment tones for preview."""
    flat = []
    for rgb in PANEL_SIM_PALETTE:
        flat.extend(rgb)
    flat.extend(PANEL_SIM_PALETTE[-1] * (256 - len(PANEL_SIM_PALETTE)))
    sim = dithered.copy()
    sim.putpalette(flat)
    return sim.convert("RGB")


def _sim_matrix() -> tuple[float, ...]:
    """The affine colour map (a 12-tuple for Image.convert) that best takes
    each DITHER_PALETTE colour to its PANEL_SIM_PALETTE tone, by weighted
    least squares: paper and black, most of any page, weigh most, so they
    come out all but exact."""
    rows = [(*rgb, 255) for rgb in DITHER_PALETTE]
    weights = [50 if rgb in ((0, 0, 0), (255, 255, 255)) else 1 for rgb in DITHER_PALETTE]
    out: list[float] = []
    for c in range(3):
        # Normal equations, 4x4, solved by Gauss-Jordan elimination.
        m = [[sum(w * r[i] * r[j] for r, w in zip(rows, weights)) for j in range(4)]
             + [sum(w * r[i] * t[c] for r, t, w in zip(rows, PANEL_SIM_PALETTE, weights))]
             for i in range(4)]
        for i in range(4):
            p = max(range(i, 4), key=lambda k: abs(m[k][i]))
            m[i], m[p] = m[p], m[i]
            for k in range(4):
                if k != i:
                    f = m[k][i] / m[i][i]
                    m[k] = [a - f * b for a, b in zip(m[k], m[i])]
        r, g, b, k = (m[i][4] / m[i][i] for i in range(4))
        out += [r, g, b, k * 255]
    return tuple(out)


SIM_MATRIX = _sim_matrix()


def simulate_tones(img: Image.Image) -> Image.Image:
    """An undithered RGB or RGBA image in the panel simulation's muted
    tones (alpha kept): what the pane shows while the print is dragged,
    before the real dithered picture comes back."""
    if img.mode == "RGBA":
        out = img.convert("RGB").convert("RGB", SIM_MATRIX)
        out.putalpha(img.getchannel("A"))
        return out
    return img.convert("RGB", SIM_MATRIX)
