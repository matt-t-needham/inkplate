import pytest

import screens


def test_every_screen_renders_at_panel_size(data_dir, fake_data):
    for name, fn in screens.SCREENS.items():
        img = fn(400, 300)  # scaled down for test speed; screens are size-agnostic
        assert img.size == (400, 300), name
        assert img.mode == "RGB", name
        img = fn(300, 400)  # portrait: the panel hung on its side
        assert img.size == (300, 400), name


def test_dashboard_portrait_layouts(data_dir, fake_data):
    import state
    assert screens.DEFAULT_PORTRAIT_LAYOUT in screens.PORTRAIT_LAYOUTS
    for layout in screens.PORTRAIT_LAYOUTS:
        state.update_config(portrait_layout=layout, show_snow=True)
        img = screens.dashboard(1200, 1600)
        assert img.size == (1200, 1600), layout
    assert screens.dashboard(1600, 1200).size == (1600, 1200)


def test_hero_centres_the_plate_in_the_top_section(data_dir, fake_data):
    """Hero: the plate is centred on its ink in the art section above the
    weather band, not bled off an edge."""
    import state
    from PIL import Image, ImageDraw
    sheet = Image.new("RGB", (900, 1200), (238, 234, 224))
    ImageDraw.Draw(sheet).rectangle([350, 300, 550, 900], fill=(30, 30, 30))
    sheet.save(fake_data["animal"]["path"])
    state.update_config(portrait_layout="hero", show_snow=True)
    img = screens.dashboard(1200, 1600)
    xs = [x for x in range(0, 1200, 4) for y in range(200, 940, 8)  # between caption and weather
          if img.getpixel((x, y))[0] < 100]
    assert xs, "no ink in the art section"
    centre = (min(xs) + max(xs)) / 2
    assert abs(centre - 600) < 20
    assert max(xs) < 1180 and min(xs) > 20     # whole figure inside the frame


def test_default_screen_registered():
    assert screens.DEFAULT_SCREEN in screens.SCREENS
    assert screens.DEFAULT_SCREEN == "dashboard"


def test_full_panel_resolution_render(data_dir, fake_data):
    img = screens.SCREENS[screens.DEFAULT_SCREEN](1600, 1200)
    assert img.size == (1600, 1200)


def test_dashboard_survives_every_source_down(data_dir, monkeypatch):
    import datasources
    monkeypatch.setattr(datasources, "get_weather", lambda lat, lon: None)
    monkeypatch.setattr(datasources, "get_animal", lambda day=None, offset=0, period_days=1, vetoed=(), keep=(): None)
    monkeypatch.setattr(datasources, "get_snow", lambda lat, lon: None)
    img = screens.dashboard(1600, 1200)
    assert img.size == (1600, 1200)  # degraded, never blank/crashed


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


def test_dashboard_print_is_opaque_at_the_border_and_fades_on_inner_edges(data_dir, fake_data):
    """Full bleed: wherever a dashboard print reaches the panel's border it
    is opaque right up to it; where it meets other content (the stack's
    weather above, the hero's weather band below) it still fades."""
    import state
    from PIL import Image
    Image.new("RGB", (900, 1200), (20, 20, 20)).save(fake_data["animal"]["path"])
    state.update_config(show_snow=False, portrait_layout="stack")
    img = screens.dashboard(1200, 1600)
    top = screens.art_box(1200, 1600, "stack", False)[0][1]
    for xy in ((0, 1000), (1199, 1000), (600, 1599), (0, 1599)):
        assert img.getpixel(xy)[0] < 40, xy                    # opaque at the border
    assert img.getpixel((600, top))[0] > 200                    # inner top edge: faded out
    assert img.getpixel((600, top + 400))[0] < 40
    state.update_config(portrait_layout="hero")
    img = screens.dashboard(1200, 1600)
    bottom = screens.art_box(1200, 1600, "hero", False)[0][3]
    for xy in ((0, 600), (1199, 600), (300, 0)):
        assert img.getpixel(xy)[0] < 40, xy
    assert img.getpixel((300, bottom - 1))[0] > 200             # against the weather band


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


def test_dashboard_snow_toggle_and_location(data_dir, fake_data, monkeypatch):
    import datasources
    import state
    calls = []
    monkeypatch.setattr(datasources, "get_snow",
                        lambda lat, lon: calls.append((lat, lon)) or fake_data["snow"])
    state.update_config(show_snow=True)
    screens.dashboard(1600, 1200)
    state.update_config(snow_latitude=46.0, snow_longitude=-121.5)
    screens.dashboard(1600, 1200)
    assert calls[-1] == (46.0, -121.5)
    state.update_config(show_snow=False)
    screens.dashboard(400, 300)
    assert len(calls) == 2  # off means not even fetched


def test_dow():
    assert screens._dow("2026-12-16") == "Wed"
    assert screens._dow(None) == "?"


def _figure_plate(w=900, h=1200):
    """A tall figure on a sheet of paper with generous margins."""
    from PIL import ImageDraw
    sheet = _sheet(w, h)
    ImageDraw.Draw(sheet).rectangle([350, 300, 550, 900], fill=(30, 30, 30))
    return sheet


def _ink_bbox(img):
    return img.convert("L").point(lambda v: 255 if v < 128 else 0).getbbox()


def test_draw_art_zooms_a_contained_figure_into_the_box():
    """A figure is leaned into rather than floated in its own paper — but
    only as far as the paper goes: the pasted plate fills the box's short
    axis, is never larger than the box, and its ink runs to within a fade
    of the box edge without being cut."""
    import tempfile
    from PIL import Image
    box = (100, 100, 1100, 1100)

    def draw():
        img = Image.new("RGB", (1200, 1200), "white")
        art = screens._draw_art(img, {"path": f.name}, box, lambda v: v,
                                lambda v: v, mode="centered")
        return art.rect, _ink_bbox(img)
    with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
        _figure_plate().save(f.name)
        zoomed, zoomed_ink = draw()
        screens.ART_ZOOM, keep = 1.0, screens.ART_ZOOM
        try:
            _, fitted_ink = draw()
        finally:
            screens.ART_ZOOM = keep
    x0, y0, x1, y1 = zoomed
    assert x0 >= box[0] and y0 >= box[1] and x1 <= box[2] and y1 <= box[3]
    assert y1 - y0 == 1000                              # fills the limiting axis
    # never smaller than a fit; here the subject box's own margin is thinner
    # than the fade, so there is no paper to spend and it stays at fit
    assert zoomed_ink[2] - zoomed_ink[0] >= fitted_ink[2] - fitted_ink[0]
    # ...and no further: the whole figure is still there, top and bottom
    assert zoomed_ink[1] >= box[1] and zoomed_ink[3] <= box[3]
    # ink stops short of the box by about the fade, so the fade is on paper
    assert zoomed_ink[3] - zoomed_ink[1] >= 1000 - 2 * screens.HERO_FADE - 12


def test_lean_in_never_cuts_a_thin_top_off_a_heavy_figure():
    """The lark: a heavy rock and plant under a thin neck and beak. The ink
    centroid sits low, so cropping a zoomed plate around it took the whole
    overflow off the top — the head. The thin top must survive."""
    import tempfile
    from PIL import Image, ImageDraw
    sheet = _sheet(900, 1200)
    d = ImageDraw.Draw(sheet)
    d.rectangle([200, 700, 700, 1000], fill=(30, 30, 30))   # rock: the mass
    d.rectangle([420, 200, 500, 700], fill=(30, 30, 30))    # neck and beak
    box = (0, 0, 1200, 900)                                 # height-limited
    with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
        sheet.save(f.name)
        img = Image.new("RGB", (1200, 900), "white")
        screens._draw_art(img, {"path": f.name}, box, lambda v: v,
                          lambda v: v, mode="centered")
    g = img.convert("L")
    top = _ink_bbox(img)[1]
    # the neck's top end is drawn, near the top of the box, in solid ink
    assert top < 60 + screens.HERO_FADE
    assert min(g.getpixel((x, top + 2)) for x in range(img.width)) < 80


def test_centered_art_fades_on_paper_margin_not_the_subject():
    """Hero art is drawn with HERO_CONTEXT of sheet around the subject where
    the box has room, so the HERO_FADE dissolves paper, not the figure: ink
    at the very edge of the subject crop stays solid."""
    import tempfile
    from PIL import Image
    box = (0, 0, 1600, 1000)                     # wide: room left and right
    with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
        _figure_plate().save(f.name)
        img = Image.new("RGB", (1600, 1000), "white")
        rect = screens._draw_art(img, {"path": f.name}, box, lambda v: v,
                                 lambda v: v, mode="centered").rect
    ink = _ink_bbox(img)
    # paper drawn beyond the figure on both sides, wider than the fade
    assert ink[0] - rect[0] > screens.HERO_FADE
    assert rect[2] - ink[2] > screens.HERO_FADE
    # the figure's own edge column is solid ink, not faded towards white
    mid = (ink[1] + ink[3]) // 2
    assert img.convert("L").getpixel((ink[0] + 1, mid)) < 80
    assert img.convert("L").getpixel((ink[2] - 2, mid)) < 80


def test_draw_art_applies_per_image_rotation():
    """animal["rotation"] (clockwise degrees) turns the plate before the crop:
    a tall figure rotated 90° lands on the panel as a wide one."""
    import tempfile
    from PIL import Image
    with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
        _figure_plate().save(f.name)
        box = (0, 0, 1000, 1000)
        upright = screens._draw_art(Image.new("RGB", (1000, 1000), "white"),
                                    {"path": f.name}, box, lambda v: v, lambda v: v).rect
        turned = screens._draw_art(Image.new("RGB", (1000, 1000), "white"),
                                   {"path": f.name, "rotation": 90}, box,
                                   lambda v: v, lambda v: v).rect
    assert upright[3] - upright[1] > upright[2] - upright[0]   # tall
    assert turned[2] - turned[0] > turned[3] - turned[1]       # wide


def test_image_settings_read_config_by_file():
    a = {"file": "File:Gorilla.tif", "path": "x"}
    cfg = {"image_rotations": {"File:Gorilla.tif": 90},
           "image_placements": {"File:Gorilla.tif": {"hero": {"x": 0.1, "y": 0, "scale": 1}}}}
    other = screens.with_image_settings({"file": "File:Other.tif"}, cfg)
    assert (other["rotation"], other["placements"]) == (0, {})
    got = screens.with_image_settings(a, cfg)
    assert (got["rotation"], got["placements"]) == (90, {"hero": {"x": 0.1, "y": 0, "scale": 1}})
    assert "rotation" not in a   # the cached dict is not mutated


def test_placement_moves_only_its_own_image_in_its_own_layout(data_dir, fake_data):
    import state
    _figure_plate().save(fake_data["animal"]["path"])
    fake_data["animal"]["file"] = "File:Fox.tif"
    state.update_config(portrait_layout="hero", show_snow=True)
    auto = screens.dashboard(1200, 1600)
    # Another image's placement, and this image's placement for the other
    # portrait layout and for landscape, leave the hero pixel for pixel.
    state.update_config(image_placements={
        "File:Other.tif": {"hero": {"x": 0.3, "y": 0, "scale": 1.5}},
        "File:Fox.tif": {"stack": {"x": 0.3, "y": 0, "scale": 1.5},
                         "landscape": {"x": -0.3, "y": 0.1, "scale": 0.8}}})
    assert screens.dashboard(1200, 1600).tobytes() == auto.tobytes()
    state.update_config(image_placements={"File:Fox.tif": {"hero": {"x": 0.2, "y": 0, "scale": 1}}})
    moved = screens.dashboard(1200, 1600)
    band = (0, 200, 1200, 900)   # under the caption, above the weather
    a, m = _ink_bbox(auto.crop(band)), _ink_bbox(moved.crop(band))
    assert m[0] - a[0] > 150 and m[2] > a[2]   # shifted right, by ~0.2 of the box


def test_placement_is_clamped_at_the_box_edge():
    """The subject's centre may go up to the box edge, never past it, so an
    image can't be pushed out of its own area; the placement reported back
    is the one drawn."""
    import tempfile
    from PIL import Image
    box = (100, 100, 1100, 1100)

    def place(p):
        img = Image.new("RGB", (1200, 1200), "white")
        art = screens._draw_art(img, {"path": f.name, "placements": {"hero": p}}, box,
                                lambda v: v, lambda v: v, mode="centered", layout="hero")
        return art.rect, _ink_bbox(img), art.placement
    with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
        _figure_plate().save(f.name)
        _, auto_ink, _ = place(None)
        centre = (auto_ink[0] + auto_ink[2]) / 2            # 600: centred
        inside = (box[2] - centre) / 1000 - 0.01            # just short of the edge
        _, _, drawn = place({"x": inside, "y": 0, "scale": 1})
        assert drawn["x"] == pytest.approx(inside, abs=1e-3)
        rect, ink, drawn = place({"x": 1.0, "y": 0, "scale": 1})
    assert drawn["x"] == pytest.approx((box[2] - centre) / 1000, abs=0.01)
    assert rect[2] <= box[2] and rect[0] >= box[0]          # never drawn past the box
    assert ink and ink[0] < box[2] - 40                     # and still there to see
