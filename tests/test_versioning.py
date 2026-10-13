"""Content-hash versioning: the device must only ever redraw on real change."""

import renderer
import state


def test_identical_content_does_not_bump_version(data_dir):
    state.update_config(screen="alignment")  # fully static screen
    v1 = renderer.render()["image_version"]
    v2 = renderer.render()["image_version"]
    assert v2 == v1  # same pixels → same version → no device redraw


def test_first_render_bumps(data_dir):
    state.update_config(screen="alignment")
    v0 = state.load_config()["image_version"]
    assert renderer.render()["image_version"] == v0 + 1


def test_force_bumps_even_when_unchanged(data_dir):
    state.update_config(screen="alignment")
    v1 = renderer.render()["image_version"]
    assert renderer.render(force_new_version=True)["image_version"] == v1 + 1


def test_content_change_bumps(data_dir):
    state.update_config(screen="alignment")
    v1 = renderer.render()["image_version"]
    state.update_config(screen="palette_test")
    assert renderer.render()["image_version"] == v1 + 1


def test_ensure_fresh_respects_refresh_cadence(data_dir, fake_data):
    state.update_config(screen="alignment", refresh_minutes=60)
    m1 = renderer.ensure_fresh()
    assert renderer.ensure_fresh()["rendered_at"] == m1["rendered_at"]  # within the hour


def test_rotation_turns_the_device_frame_not_the_preview(data_dir):
    """The device always gets 1600x1200; the previews are upright. Direction:
    rotation is the panel's clockwise turn, so what is drawn top-left in the
    upright frame must end up bottom-left in the native frame (which is where
    the native top-left corner goes when the panel turns clockwise)."""
    from PIL import Image
    state.update_config(screen="alignment", rotation=90)
    m = renderer.render()
    assert m["rotation"] == 90
    raw = Image.open(renderer.preview_path("raw"))
    frame = Image.open(renderer.dithered_path())
    assert raw.size == (1200, 1600)
    assert frame.size == (1600, 1200)
    assert frame.mode == "P"
    # alignment draws a red inner border 10px in; sample the "(0,0)" label's
    # black ink near the upright top-left and find it at the frame's
    # bottom-left after the turn.
    up = Image.new("RGB", (1200, 1600), (255, 255, 255))
    up.putpixel((0, 0), (0, 0, 0))
    turned = renderer.to_panel_frame(up, 90)
    assert turned.size == (1600, 1200)
    assert turned.getpixel((0, 1199)) == (0, 0, 0)
    assert renderer.to_panel_frame(up, 0) is up
    assert renderer.upright_size(270) == (1200, 1600)
    assert renderer.upright_size(180) == (1600, 1200)


def test_rotation_change_bumps_version_and_rerenders(data_dir):
    state.update_config(screen="alignment", rotation=0)
    m1 = renderer.ensure_fresh()
    assert renderer.ensure_fresh()["rendered_at"] == m1["rendered_at"]
    state.update_config(rotation=180)
    m2 = renderer.ensure_fresh()          # stale: the hanging changed
    assert m2["rendered_at"] != m1["rendered_at"]
    assert m2["image_version"] == m1["image_version"] + 1   # device must redraw
