import screens


def test_every_screen_renders_at_panel_size(data_dir, fake_data):
    for name, fn in screens.SCREENS.items():
        img = fn(400, 300)  # scaled down for test speed; screens are size-agnostic
        assert img.size == (400, 300), name
        assert img.mode == "RGB", name


def test_default_screen_registered():
    assert screens.DEFAULT_SCREEN in screens.SCREENS
    assert screens.DEFAULT_SCREEN == "dashboard"


def test_full_panel_resolution_render(data_dir, fake_data):
    img = screens.SCREENS[screens.DEFAULT_SCREEN](1600, 1200)
    assert img.size == (1600, 1200)


def test_dashboard_survives_every_source_down(data_dir, monkeypatch):
    import datasources
    monkeypatch.setattr(datasources, "get_weather", lambda lat, lon: None)
    monkeypatch.setattr(datasources, "get_animal", lambda day=None, offset=0, period_days=1: None)
    monkeypatch.setattr(datasources, "get_now_playing", lambda: None)
    img = screens.dashboard(1600, 1200)
    assert img.size == (1600, 1200)  # degraded, never blank/crashed


def test_dashboard_now_playing_only_when_enabled(data_dir, fake_data, monkeypatch):
    import state

    calls = []
    import datasources
    real = datasources.get_now_playing
    monkeypatch.setattr(datasources, "get_now_playing",
                        lambda: calls.append(1) or real())

    state.update_config(show_now_playing=False)
    screens.dashboard(400, 300)
    assert calls == []

    state.update_config(show_now_playing=True)
    screens.dashboard(400, 300)
    assert calls == [1]


def test_weather_icon_kinds_all_drawable():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (200, 200), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for kind in ("sun", "partly", "cloud", "fog", "rain", "snow", "storm", "??"):
        screens._weather_icon(d, 100, 100, 40, kind)


def test_c_to_f():
    assert screens._c_to_f(0) == 32
    assert screens._c_to_f(100) == 212
    assert screens._c_to_f(26.8) == 80


def _sheet(w=900, h=1200, paper=(238, 234, 224)):
    from PIL import Image
    return Image.new("RGB", (w, h), paper)


def test_prep_engraving_crops_to_the_figure_not_the_caption():
    """A figure plus a caption strip below it: the crop must keep the figure
    and drop the caption. An absolute bounding box keeps both — which is the
    bug this replaced."""
    from PIL import Image, ImageDraw
    page = _sheet()
    d = ImageDraw.Draw(page)
    d.ellipse([300, 200, 600, 700], fill=(30, 30, 30))          # the figure
    for i in range(4):                                           # caption lines
        d.rectangle([280, 950 + i * 22, 620, 956 + i * 22], fill=(40, 40, 40))
    d.text((820, 40), "note", fill=(50, 50, 50))                 # marginalia
    out, is_scene = screens._prep_engraving(page)
    assert not is_scene
    assert out.height < 700          # caption strip excluded
    assert out.width < 700
    # Paper must be clean enough that the dither maps it to white rather than
    # speckling the background with colour — assert that, not a magic value.
    import palette
    assert palette.dither_to_spectra(out).convert("RGB").getpixel((2, 2)) == (255, 255, 255)


def test_prep_engraving_survives_dark_scan_borders():
    """Near-black scan edges are denser than the figure; without trimming
    them first the crop locks onto the border and returns a sliver."""
    from PIL import Image, ImageDraw
    page = _sheet()
    d = ImageDraw.Draw(page)
    d.rectangle([0, 0, 60, 1199], fill=(8, 8, 8))       # dark left edge
    d.rectangle([840, 0, 899, 1199], fill=(8, 8, 8))    # dark right edge
    d.ellipse([320, 300, 560, 800], fill=(30, 30, 30))  # the figure
    out, _ = screens._prep_engraving(page)
    assert out.width < 700 and out.height < 900
    # the returned crop is the figure, not a black strip
    assert out.width > 120 and out.height > 120


def test_scene_vs_figure_detection():
    from PIL import ImageDraw
    figure = _sheet()
    ImageDraw.Draw(figure).ellipse([350, 400, 550, 700], fill=(30, 30, 30))
    assert screens._prep_engraving(figure)[1] is False

    # A real scene plate is dense *hatching*, not a solid block: the paper
    # tone estimate assumes paper still wins the histogram, which holds for
    # every plate in these corpuses (the densest measured is ~37% ink).
    scene = _sheet()
    sd = ImageDraw.Draw(scene)
    for y in range(100, 1100, 4):
        sd.line([60, y, 840, y], fill=(50, 50, 50), width=2)
    assert screens._prep_engraving(scene)[1] is True


def test_dominant_run_prefers_the_inkiest_band():
    prof = [0] * 20 + [50] * 40 + [0] * 20 + [3] * 30   # dense band, sparse tail
    a, b = screens._dominant_run(prof)
    assert 20 <= a <= 21 and 58 <= b <= 60


def test_edge_fade_mask_per_side():
    m = screens._edge_fade_mask(200, 100, left=0.3, right=0.0, top=0.2, bottom=0.2)
    assert m.size == (200, 100)
    mid_y = 50
    assert m.getpixel((0, mid_y)) == 0          # left edge fully transparent
    assert m.getpixel((199, mid_y)) == 255      # right edge hard (no fade)
    assert m.getpixel((100, mid_y)) == 255      # interior opaque
    assert m.getpixel((150, 0)) == 0            # top still fades
    assert m.getpixel((150, 99)) == 0           # bottom still fades
    # left fade is monotonic across its 30%
    ramp = [m.getpixel((x, mid_y)) for x in range(0, 60)]
    assert ramp == sorted(ramp) and ramp[-1] > ramp[0]


def test_all_glyph_kinds_render():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (300, 300), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for kind in list(screens.WI_GLYPHS) + ["nonsense"]:
        w = screens._weather_icon(d, 150, 150, 40, kind)
        assert w > 0, kind
        assert screens._weather_icon_width(d, 40, kind) > 0, kind


def test_weather_font_actually_loaded():
    """A missing font would silently fall back to a default face and draw
    tofu — check the real file resolved."""
    from PIL import ImageFont
    f = screens._wi_font(40)
    assert isinstance(f, ImageFont.FreeTypeFont)
    assert "weathericons" in f.path.lower()
