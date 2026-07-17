"""Tests for hokku_setup.py — cross-platform import (Blocker B regression),
platform-gated menu visibility, and action dispatchability.
"""

import inspect
import os
import sys
import unittest.mock as um

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import hokku_setup as hs


def test_imports_on_non_windows():
    """The whole wizard must import even when ctypes.wintypes is unavailable —
    this is the Blocker B regression (pi_installer used to import wintypes at
    module top level, killing `import hokku_setup` on Linux/macOS)."""
    # If we got here, `import hokku_setup` (which imports pi_installer and
    # local_installer) already succeeded on this platform. Assert the module
    # object and its installers are present.
    assert hasattr(hs, "pi_installer")
    assert hasattr(hs, "local_installer")


def test_pi_options_hidden_off_windows():
    with um.patch.object(hs.sys, "platform", "linux"):
        ids = [aid for aid, _ in hs._visible_actions()]
    assert "pi_full" not in ids
    assert "pi_only" not in ids
    assert "local_full" in ids
    assert "esp_both" in ids


def test_pi_options_visible_on_windows():
    with um.patch.object(hs.sys, "platform", "win32"):
        ids = [aid for aid, _ in hs._visible_actions()]
    assert "pi_full" in ids and "pi_only" in ids


def test_every_visible_action_id_is_dispatchable():
    src = inspect.getsource(hs._dispatch)
    for platform in ("win32", "linux"):
        with um.patch.object(hs.sys, "platform", platform):
            for aid, _ in hs._visible_actions():
                assert aid in src, f"{aid} not handled in _dispatch"


def test_menu_defaults_by_device_state():
    assert hs._menu_default(None) == "local_full"
    assert hs._menu_default({"device": {"has_hokku_firmware": False}}) == "esp_both"
    assert (
        hs._menu_default({"device": {"has_hokku_firmware": True, "config_version_ok": False}})
        == "esp_config"
    )
    assert (
        hs._menu_default(
            {
                "device": {
                    "has_hokku_firmware": True,
                    "config_version_ok": True,
                    "firmware_current": False,
                }
            }
        )
        == "esp_flash"
    )
