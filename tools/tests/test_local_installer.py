"""Tests for local_installer.py — config generation, Scheduled Task XML, firewall
argv, LAN-IP ranking, and the mDNS-vs-IP addressing decision.

Every schtasks/netsh call goes through local_installer._run, which these tests
monkeypatch, so nothing here touches the real system.
"""

import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import esp32_setup
import local_installer as li

_TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


class TestConfigJson:
    def test_config_carries_version(self, tmp_path):
        images, cache = tmp_path / "images", tmp_path / "cache"
        path = li.write_server_config(tmp_path, images, cache, 8080, "hokku")
        data = json.loads(path.read_text(encoding="utf-8"))
        # The version key is the landmine: without it the server discards
        # everything and exits on the default /var/lib path.
        assert "version" in data
        assert isinstance(data["version"], int)

    def test_config_roundtrips_paths(self, tmp_path):
        images, cache = tmp_path / "images", tmp_path / "cache"
        path = li.write_server_config(tmp_path, images, cache, 9099, "myframe")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["upload_dir"] == images.as_posix()
        assert data["cache_dir"] == cache.as_posix()
        assert data["port"] == 9099
        assert data["mdns_hostname"] == "myframe"

    def test_config_version_matches_server(self):
        """local_installer's fallback version must match the server's real one."""
        version = li._current_config_version()
        # Read the server's _CURRENT_VERSION straight from source (no import).
        src = (li.WEBSERVER_DIR / "hokku_server" / "app_config.py").read_text(encoding="utf-8")
        real = int(re.search(r"^_CURRENT_VERSION\s*=\s*(\d+)", src, re.MULTILINE).group(1))
        assert version == real
        assert li._FALLBACK_CONFIG_VERSION == real


class TestTaskXml:
    def _xml(self):
        return li.build_task_xml(
            "C:/repo/.venv/Scripts/python.exe",
            "D:/Hokku/config.json",
            "C:/repo/webserver",
            "D:/Hokku/logs/server.log",
        )

    def test_is_utf16(self):
        raw = self._xml()
        # UTF-16 decodes; UTF-8 strict decode of UTF-16-with-BOM would raise.
        assert raw.decode("utf-16").startswith("<?xml")

    def test_parses_and_has_boot_system_principal(self):
        root = ET.fromstring(self._xml().decode("utf-16"))  # noqa: S314 — our own trusted XML
        assert root.find(".//t:BootTrigger", _TASK_NS) is not None
        assert root.find(".//t:Principal/t:UserId", _TASK_NS).text == "S-1-5-18"

    def test_action_redirects_to_log(self):
        root = ET.fromstring(self._xml().decode("utf-16"))  # noqa: S314 — our own trusted XML
        args = root.find(".//t:Arguments", _TASK_NS).text
        assert "2>&1" in args  # entities unescaped by the parser
        assert '>> "D:/Hokku/logs/server.log"' in args
        assert "-m hokku_server" in args
        assert root.find(".//t:Command", _TASK_NS).text == "cmd.exe"


class TestSchtasks:
    def test_register_argv(self, monkeypatch, tmp_path):
        monkeypatch.setattr(li.release_cache, "CACHE_DIR", tmp_path)
        calls = []
        monkeypatch.setattr(li, "_run", lambda argv, **k: calls.append(argv) or _ok())
        ok = li.register_scheduled_task("py.exe", "cfg.json", "workdir", "log.txt", name="T")
        assert ok
        argv = calls[-1]
        assert argv[0] == "schtasks" and "/Create" in argv and "/XML" in argv
        assert "/F" in argv and "T" in argv and "SYSTEM" in argv

    def test_register_creates_missing_cache_dir(self, monkeypatch, tmp_path):
        # On a fresh clone .cache/ doesn't exist yet; register must create it
        # rather than crash writing the task XML into a missing directory.
        missing = tmp_path / "does-not-exist-yet"
        monkeypatch.setattr(li.release_cache, "CACHE_DIR", missing)
        monkeypatch.setattr(li, "_run", lambda argv, **k: _ok())
        assert li.register_scheduled_task("py.exe", "cfg.json", "wd", "log.txt", name="T")
        assert (missing / "hokku_task.xml").exists()

    def test_unregister_when_absent_is_noop(self, monkeypatch):
        monkeypatch.setattr(li, "task_exists", lambda name=li.TASK_NAME: False)
        called = []
        monkeypatch.setattr(li, "_run", lambda argv, **k: called.append(argv) or _ok())
        assert li.unregister_scheduled_task("T") is True
        assert called == []  # nothing to delete


class TestFirewall:
    def test_argv_tcp_and_mdns_private_only(self, monkeypatch):
        calls = []
        monkeypatch.setattr(li, "_run", lambda argv, **k: calls.append(argv) or _ok())
        names = li.add_firewall_rules(8080, "C:/py.exe")
        assert names == ["Hokku Server (TCP 8080)", "Hokku Server mDNS (UDP 5353)"]
        add_calls = [c for c in calls if "add" in c]
        joined = [" ".join(c) for c in add_calls]
        assert any("protocol=TCP" in j and "localport=8080" in j for j in joined)
        assert any("protocol=UDP" in j and "localport=5353" in j for j in joined)
        # never expose on public networks
        for j in joined:
            assert "profile=private,domain" in j
            assert "profile=any" not in j

    def test_deletes_before_add(self, monkeypatch):
        calls = []
        monkeypatch.setattr(li, "_run", lambda argv, **k: calls.append(argv) or _ok())
        li.add_firewall_rules(8080, "C:/py.exe")
        # netsh advfirewall firewall <verb> ...  -> verb is argv[3]
        verbs = [c[3] for c in calls]
        assert verbs[0] == "delete" and verbs[2] == "delete"


class TestLanIpRanking:
    def test_skips_virtual_adapters(self):
        ifaces = [
            ("vEthernet (WSL)", "172.28.144.1"),
            ("Wi-Fi", "192.168.1.16"),
            ("Tailscale", "10.7.18.47"),
        ]
        ranked = li._rank_candidates(ifaces, route_ip="192.168.1.16")
        assert ranked[0]["ip"] == "192.168.1.16"
        assert ranked[0]["virtual"] is False

    def test_skips_loopback_and_link_local(self):
        ifaces = [("lo", "127.0.0.1"), ("apipa", "169.254.1.2"), ("Ethernet", "10.0.0.5")]
        ranked = li._rank_candidates(ifaces, route_ip=None)
        assert [c["ip"] for c in ranked] == ["10.0.0.5"]

    def test_rfc1918(self):
        assert li._is_rfc1918("192.168.1.1")
        assert li._is_rfc1918("172.20.0.1")
        assert li._is_rfc1918("10.1.2.3")
        assert not li._is_rfc1918("8.8.8.8")
        assert not li._is_rfc1918("172.15.0.1")


class TestResolveServerHost:
    def test_prefers_mdns_when_it_points_at_us(self, monkeypatch):
        monkeypatch.setattr(
            esp32_setup, "_check_server_reachable", lambda host, port: (True, "192.168.1.16")
        )
        host = li.resolve_server_host("hokku", 8080, "192.168.1.16")
        assert host == "hokku.local"

    def test_falls_back_on_hostname_collision(self, monkeypatch):
        # hokku.local resolves to a DIFFERENT machine → must use the IP.
        monkeypatch.setattr(
            esp32_setup, "_check_server_reachable", lambda host, port: (True, "192.168.1.99")
        )
        monkeypatch.setattr(li, "_own_ipv4_addresses", lambda: {"192.168.1.16"})
        host = li.resolve_server_host("hokku", 8080, "192.168.1.16")
        assert host == "192.168.1.16"

    def test_falls_back_when_advertised_on_own_vpn_adapter(self, monkeypatch):
        # hokku.local resolves to THIS PC's VPN IP (own address, not the LAN IP)
        # → still fall back to the LAN IP the frame can actually reach.
        monkeypatch.setattr(
            esp32_setup, "_check_server_reachable", lambda host, port: (True, "10.7.18.47")
        )
        monkeypatch.setattr(li, "_own_ipv4_addresses", lambda: {"192.168.1.16", "10.7.18.47"})
        host = li.resolve_server_host("hokku", 8080, "192.168.1.16")
        assert host == "192.168.1.16"

    def test_falls_back_when_unresolvable(self, monkeypatch):
        monkeypatch.setattr(
            esp32_setup, "_check_server_reachable", lambda host, port: (False, None)
        )
        host = li.resolve_server_host("hokku", 8080, "192.168.1.16")
        assert host == "192.168.1.16"

    def test_empty_hostname_uses_ip(self):
        assert li.resolve_server_host("", 8080, "192.168.1.16") == "192.168.1.16"


class TestState:
    def test_is_installed_false_when_task_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(li, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(li, "task_exists", lambda name=li.TASK_NAME: False)
        assert li.is_installed() is False

    def test_uninstall_removes_task_and_rules(self, monkeypatch, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "task_name": "HokkuServer",
                    "install_dir": str(tmp_path),
                    "port": 8080,
                    "firewall_rules": ["Hokku Server (TCP 8080)", "Hokku Server mDNS (UDP 5353)"],
                }
            )
        )
        monkeypatch.setattr(li, "STATE_FILE", state_file)
        monkeypatch.setattr(li.sys, "platform", "win32")
        monkeypatch.setattr(li, "_is_admin", lambda: True)
        monkeypatch.setattr(li, "task_exists", lambda name=li.TASK_NAME: True)
        removed = []
        monkeypatch.setattr(li, "_run", lambda argv, **k: removed.append(argv) or _ok())
        rc = li.uninstall()
        assert rc == 0
        flat = " ".join(" ".join(c) for c in removed)
        assert "/Delete" in flat
        assert "Hokku Server (TCP 8080)" in flat
        assert "Hokku Server mDNS (UDP 5353)" in flat
        assert not state_file.exists()


def _ok():
    """A CompletedProcess-like stub with returncode 0."""
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
