from PIL import Image

import palette


def test_dither_output_only_spectra_colors():
    # A photo-ish gradient forces heavy dithering; every output pixel must
    # still be one of the six legal colors.
    img = Image.new("RGB", (120, 80))
    img.putdata([(x * 2, y * 3, (x + y)) for y in range(80) for x in range(120)])
    dithered = palette.dither_to_spectra(img)
    assert dithered.mode == "P"
    rgb = dithered.convert("RGB")
    used = {px for px in rgb.getdata()}
    assert used.issubset(set(palette.DITHER_PALETTE))


def test_dither_preserves_size():
    img = Image.new("RGB", (33, 47), (128, 128, 128))
    assert palette.dither_to_spectra(img).size == (33, 47)


def test_pure_colors_map_to_themselves():
    for rgb in palette.DITHER_PALETTE:
        img = Image.new("RGB", (10, 10), rgb)
        out = palette.dither_to_spectra(img).convert("RGB")
        assert set(out.getdata()) == {rgb}


def test_simulate_panel_uses_sim_palette():
    img = Image.new("RGB", (20, 20), (255, 0, 0))
    sim = palette.simulate_panel(palette.dither_to_spectra(img))
    assert sim.mode == "RGB"
    assert set(sim.getdata()) == {tuple(palette.PANEL_SIM_PALETTE[3])}
