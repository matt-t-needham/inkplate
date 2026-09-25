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


def test_ensure_fresh_uses_fast_ttl_with_now_playing(data_dir, fake_data, monkeypatch):
    import config
    import time as _time
    state.update_config(screen="alignment", refresh_minutes=60)
    m1 = renderer.ensure_fresh()

    # Within the hour: no re-render normally.
    assert renderer.ensure_fresh()["rendered_at"] == m1["rendered_at"]

    # With now-playing on, the render TTL shrinks to NOW_PLAYING_RENDER_TTL_S.
    state.update_config(show_now_playing=True)
    monkeypatch.setattr(config, "NOW_PLAYING_RENDER_TTL_S", 0)
    m2 = renderer.ensure_fresh()
    assert m2["rendered_at"] >= m1["rendered_at"]
    assert m2["rendered_at"] != m1["rendered_at"]
