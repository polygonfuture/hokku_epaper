"""Committing a photo edit must evict that photo's cached mono renders.

The E1003 mono frame renders live from an in-process cache (mono_e1003._cache) keyed on
the source path + edit params. An edit that mono inherits (edit_crop_mono is None → follows
the colour edit_crop) changes only metadata, not the source file, so the manager must
actively invalidate the mono cache on commit — otherwise the mono frame serves the pre-edit
render until an unrelated cache clear (the "stale mono until you clear cache" bug).
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from hokku_server import mono_e1003, mono_tone


@pytest.fixture
def photo(tmp_path):
    p = tmp_path / "shot.png"
    a = np.zeros((900, 1400, 3), np.uint8)
    a[:300, :] = 200
    Image.fromarray(a, "RGB").save(p)
    return p


def test_invalidate_drops_all_entries_for_a_path(photo):
    mono_e1003._cache.clear()
    mono_e1003.render_mono_bin(photo, frame_portrait=False)
    mono_e1003.render_mono_bin(photo, frame_portrait=False, rotation_quarters=1,
                               crop_rect=(0.0, 0.19, 1.0, 0.61))
    assert len(mono_e1003._cache) == 2
    dropped = mono_e1003.invalidate(photo)
    assert dropped == 2
    assert len(mono_e1003._cache) == 0


def test_invalidate_leaves_other_photos_untouched(photo, tmp_path):
    other = tmp_path / "other.png"
    Image.fromarray(np.full((800, 800, 3), 128, np.uint8), "RGB").save(other)
    mono_e1003._cache.clear()
    mono_e1003.render_mono_bin(photo, frame_portrait=False)
    mono_e1003.render_mono_bin(other, frame_portrait=False)
    assert len(mono_e1003._cache) == 2
    mono_e1003.invalidate(photo)
    # only `photo`'s entry is gone; `other` survives
    assert len(mono_e1003._cache) == 1
    remaining = next(iter(mono_e1003._cache))
    assert remaining[0] == str(other)


def test_invalidate_clears_staged_preview_cache(photo):
    mono_tone._disp_cache.clear()
    # a staged-preview id_key starts with str(path) (as render_mono_preview_image builds it)
    id_key = (str(photo), 12345, 900, 675, 0, None, False, False, 0.0)
    ck = ((id_key), None, None)
    mono_tone._disp_cache[ck] = np.zeros((4, 4), np.uint8)
    assert len(mono_tone._disp_cache) == 1
    mono_tone.invalidate(photo)
    assert len(mono_tone._disp_cache) == 0


def test_invalidate_missing_path_is_safe(tmp_path):
    mono_e1003._cache.clear()
    assert mono_e1003.invalidate(tmp_path / "never_rendered.png") == 0
