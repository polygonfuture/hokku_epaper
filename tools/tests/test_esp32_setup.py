"""Tests for esp32_setup.py — credential pre-fill and config-mismatch logic.

Regression coverage for the wifi pre-fill bug: server installers hand credentials
under the sticky keys ``wifi_ssid`` / ``wifi_pass``, but prompt_config used to read
only ``wifi_ssid1`` / ``wifi_pass1``, so pre-fill silently never happened.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import esp32_setup


def _all_enter(monkeypatch):
    """Drive prompt_config non-interactively: every input() returns '' (keep
    defaults), and the server-reachability probe reports success so its retry
    loop breaks immediately instead of doing real network I/O."""
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    monkeypatch.setattr(esp32_setup, "_check_server_reachable", lambda host, port: (True, None))


class TestPromptConfigPrefill:
    def test_prefills_wifi_from_sticky_keys(self, monkeypatch):
        """Credentials under wifi_ssid/wifi_pass (installer sticky keys) must pre-fill."""
        _all_enter(monkeypatch)
        creds = {"wifi_ssid": "MyNet", "wifi_pass": "secretpass", "server_ip": "192.168.1.50"}
        cfg = esp32_setup.prompt_config(existing_config=None, pi_credentials=creds)
        assert cfg is not None
        assert cfg["wifi_ssid1"] == "MyNet"
        assert cfg["wifi_pass1"] == "secretpass"
        assert cfg["image_url"] == "http://192.168.1.50:8080/hokku/screen/"

    def test_prefills_wifi_from_legacy_keys(self, monkeypatch):
        """The legacy wifi_ssid1/wifi_pass1 shape must still pre-fill."""
        _all_enter(monkeypatch)
        creds = {"wifi_ssid1": "LegacyNet", "wifi_pass1": "legacypass"}
        cfg = esp32_setup.prompt_config(existing_config=None, pi_credentials=creds)
        assert cfg is not None
        assert cfg["wifi_ssid1"] == "LegacyNet"
        assert cfg["wifi_pass1"] == "legacypass"

    def test_server_ip_prefills_image_url(self, monkeypatch):
        """server_ip should populate the host portion of image_url."""
        _all_enter(monkeypatch)
        creds = {"wifi_ssid": "N", "wifi_pass": "password1", "server_ip": "hokku.local"}
        cfg = esp32_setup.prompt_config(existing_config=None, pi_credentials=creds)
        assert cfg["image_url"] == "http://hokku.local:8080/hokku/screen/"


class TestConfigMismatch:
    def test_mismatch_detected_with_sticky_keys(self):
        existing = {"wifi_ssid1": "OldNet", "wifi_pass1": "oldpass"}
        creds = {"wifi_ssid": "NewNet", "wifi_pass": "newpass"}
        diffs = esp32_setup._pi_config_mismatch(existing, creds)
        assert "wifi_ssid1" in diffs
        assert "wifi_pass1" in diffs

    def test_no_mismatch_when_equal(self):
        existing = {
            "wifi_ssid1": "Net",
            "wifi_pass1": "pass",
            "image_url": "http://1.2.3.4:8080/hokku/screen/",
        }
        creds = {"wifi_ssid": "Net", "wifi_pass": "pass", "server_ip": "1.2.3.4"}
        assert esp32_setup._pi_config_mismatch(existing, creds) == []

    def test_server_ip_mismatch(self):
        existing = {"image_url": "http://1.2.3.4:8080/hokku/screen/"}
        creds = {"server_ip": "5.6.7.8"}
        assert "server_ip" in esp32_setup._pi_config_mismatch(existing, creds)
