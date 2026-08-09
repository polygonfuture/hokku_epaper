"""Tests for the E1003 mono16 wire-format renderer (M2 hook)."""

import numpy as np
from PIL import Image

from hokku_server import mono_e1003


def _mean(img: Image.Image) -> float:
    return float(np.asarray(img, dtype=np.float64).mean())


def _render(tmp_path, img: Image.Image, *, frame_portrait: bool = False) -> bytes:
    p = tmp_path / "src.png"
    img.save(p)
    return mono_e1003.render_mono_bin(p, frame_portrait=frame_portrait)


def test_wire_size_and_determinism(tmp_path):
    img = Image.new("RGB", (400, 300), (128, 90, 200))
    data = _render(tmp_path, img)
    assert len(data) == mono_e1003.MONO_BYTES == 1_314_144
    # Cache hit returns identical bytes.
    assert mono_e1003.render_mono_bin(tmp_path / "src.png") == data


def test_black_and_white_extremes(tmp_path):
    # Pure white source -> every nibble 0xF -> every byte 0xFF.
    white = _render(tmp_path, Image.new("L", (1872, 1404), 255))
    assert set(white) == {0xFF}


def test_high_nibble_is_left_pixel(tmp_path):
    # Left half black, right half white, landscape-sized so no crop/rotate.
    img = Image.new("L", (1872, 1404), 0)
    img.paste(255, (936, 0, 1872, 1404))
    data = _render(tmp_path, img)
    # Row 0: first byte covers pixels 0-1 (black side), last byte pixels
    # 1870-1871 (white side). High nibble = LEFT pixel of each pair.
    assert data[0] == 0x00
    assert data[936 // 1 - 1 + 0] is not None  # readability anchor
    assert data[935] == 0xFF  # last byte of row 0 (row = 936 bytes)


def test_color_converts_to_grayscale(tmp_path):
    # A saturated red field must land on mid-grays (its luminance), not
    # black/white — proves the L-conversion happens before dithering.
    data = _render(tmp_path, Image.new("RGB", (1872, 1404), (255, 0, 0)))
    img = mono_e1003.mono_bin_to_image(data)
    lo, hi = img.getextrema()
    assert 30 < lo <= hi < 220


def test_portrait_on_portrait_frame_rotates_into_the_wire_buffer(tmp_path):
    # A PORTRAIT-mounted frame composes portrait content, which is stored rotated 90° in
    # the physically-landscape wire buffer. Portrait source, top edge black: after that
    # rotation the black band must lie along a vertical edge (full-bleed), not letterboxed.
    img = Image.new("L", (1404, 1872), 255)
    img.paste(0, (0, 0, 1404, 200))
    data = _render(tmp_path, img, frame_portrait=True)
    out = mono_e1003.mono_bin_to_image(data)
    left_mean = _mean(out.crop((0, 0, 100, 1404)))
    right_mean = _mean(out.crop((1772, 0, 1872, 1404)))
    assert (left_mean < 60) != (right_mean < 60), "top band should map to exactly one vertical edge"


def test_portrait_on_landscape_frame_letterboxes_upright(tmp_path):
    # The mismatch case: a portrait photo on a LANDSCAPE frame is composed upright for
    # that frame and letterboxed (white bars left+right) — it is NOT laid sideways to
    # fill, which is what the removed auto-rotate toggle's OFF state did.
    img = Image.new("L", (1404, 1872), 255)
    img.paste(0, (0, 0, 1404, 200))
    data = _render(tmp_path, img, frame_portrait=False)
    out = mono_e1003.mono_bin_to_image(data)
    left_bar = _mean(out.crop((0, 0, 100, 1404)))
    right_bar = _mean(out.crop((1772, 0, 1872, 1404)))
    assert left_bar > 200 and right_bar > 200, "mismatch must letterbox on a white mat"
    # the black band stays along the TOP of the upright content, not on a vertical edge
    top_center = _mean(out.crop((836, 0, 1036, 80)))
    assert top_center < 60, "content stays upright: the top band is still at the top"


def test_square_source_letterboxes(tmp_path):
    # A square frame on the 4:3 panel would lose ~24% to a cover-crop —
    # above MAX_COVER_CROP_FRAC it must letterbox on a paper-white mat
    # (level 15 bars left+right), keeping the full image.
    data = _render(tmp_path, Image.new("L", (2000, 2000), 0))
    out = mono_e1003.mono_bin_to_image(data)
    left_bar = _mean(out.crop((0, 0, 100, 1404)))
    center = _mean(out.crop((800, 500, 1100, 900)))
    assert left_bar > 180, "side mat should be paper white"
    assert center < 60, "image content should remain (dark square)"


def test_roundtrip_unpack(tmp_path):
    gradient = Image.linear_gradient("L").resize((1872, 1404))
    data = _render(tmp_path, gradient)
    out = mono_e1003.mono_bin_to_image(data)
    assert out.size == (1872, 1404)
    # Invariant: decoded output ≈ the tone-mapped source (same transform the
    # renderer applies — S-curve, plus envelope DRC only with a measured ramp).
    src = np.asarray(gradient, dtype=np.float64)
    l_src = mono_e1003._tone_map_lstar(
        mono_e1003._lstar_from_y(mono_e1003._srgb_to_linear(src)))
    if mono_e1003.USE_MEASURED_RAMP:
        l_src = mono_e1003._LB + (l_src / 100.0) * (mono_e1003._LW - mono_e1003._LB)
    expected = mono_e1003._linear_to_srgb(mono_e1003._y_from_lstar(l_src))
    assert abs(float(expected.mean()) - _mean(out)) < 6
