"""mDNS advertiser + watchdog (hokku_server.mdns).

The watchdog exists because Windows silently drops a socket's 224.0.0.251 group
membership when Wi-Fi or a VPN adapter bounces: the zeroconf responder stays
alive, answers unicast, but is deaf to every multicast query on the LAN, and the
frame gets "Image download failed" until the server restarts. These tests pin
the decision logic with injected fakes; ``test_probe_answers_live_responder``
exercises the real socket path against a real Zeroconf when the host has a LAN.
"""

from __future__ import annotations

import socket
import struct
import time

import pytest

from hokku_server import mdns


class FakeZc:
    def __init__(self) -> None:
        self.closed = False
        self.unregistered = False

    def unregister_all_services(self) -> None:
        self.unregistered = True

    def close(self) -> None:
        self.closed = True


def _make(probe_results: list[bool], *, ip: str = "10.0.0.5", register_ok: bool = True):
    """Advertiser wired to a scripted probe and a recording registrar."""
    created: list[FakeZc] = []
    results = list(probe_results)

    def register(port: int, hostname: str):
        if not register_ok:
            return None
        zc = FakeZc()
        created.append(zc)
        return zc, ip

    def probe(hostname: str, local_ip: str) -> bool:
        assert hostname == "frame"
        assert local_ip == ip
        return results.pop(0) if results else True

    adv = mdns.MdnsAdvertiser(
        8080, "frame", interval=3600, register=register, probe=probe, get_ip=lambda: ip
    )
    return adv, created


def test_start_registers_once_and_reports_ok():
    adv, created = _make([])
    assert adv.start() is True
    try:
        assert len(created) == 1
        assert adv.zc is created[0]
        assert adv.local_ip == "10.0.0.5"
        assert adv.rebuilds == 0
    finally:
        adv.close()


def test_single_miss_does_not_rebuild():
    adv, created = _make([False, True])
    adv.start()
    try:
        assert adv.check_once() is None
        assert adv.misses == 1
        assert adv.check_once() is None
        assert adv.misses == 0
        assert adv.rebuilds == 0
        assert len(created) == 1
        assert created[0].closed is False
    finally:
        adv.close()


def test_two_consecutive_misses_rebuild_and_close_old_instance():
    adv, created = _make([False, False, True])
    adv.start()
    try:
        assert adv.check_once() is None
        reason = adv.check_once()
        assert reason is not None
        assert "unanswered" in reason
        assert adv.rebuilds == 1
        assert adv.misses == 0
        assert len(created) == 2
        assert created[0].unregistered and created[0].closed
        assert adv.zc is created[1]
        assert created[1].closed is False
        # Healthy again: no further rebuilds.
        assert adv.check_once() is None
        assert adv.rebuilds == 1
    finally:
        adv.close()


def test_lan_ip_change_rebuilds_without_probing():
    ips = iter(["10.0.0.5", "10.0.0.9"])
    created: list[FakeZc] = []

    def register(port, hostname):
        zc = FakeZc()
        created.append(zc)
        return zc, next(ips)

    def probe(hostname, local_ip):
        raise AssertionError("probe must not run when the IP has changed")

    adv = mdns.MdnsAdvertiser(
        8080, "frame", interval=3600, register=register, probe=probe, get_ip=lambda: "10.0.0.9"
    )
    adv.start()
    try:
        reason = adv.check_once()
        assert reason is not None
        assert "10.0.0.5 -> 10.0.0.9" in reason
        assert adv.local_ip == "10.0.0.9"
        assert created[0].closed
        assert adv.zc is created[1]
    finally:
        adv.close()


def test_failed_registration_keeps_retrying_each_tick():
    attempts: list[int] = []
    zcs: list[FakeZc] = []

    def register(port, hostname):
        attempts.append(1)
        if len(attempts) < 3:
            return None
        zc = FakeZc()
        zcs.append(zc)
        return zc, "10.0.0.5"

    adv = mdns.MdnsAdvertiser(
        8080, "frame", interval=3600, register=register, probe=lambda h, i: True, get_ip=lambda: "10.0.0.5"
    )
    assert adv.start() is False
    try:
        assert adv.zc is None
        assert adv.check_once() == "no live advertisement"
        assert adv.zc is None  # second attempt also failed
        assert adv.check_once() == "no live advertisement"
        assert adv.zc is zcs[0]  # third attempt succeeded
        assert adv.check_once() is None
        assert len(attempts) == 3
    finally:
        adv.close()


def test_stop_mdns_accepts_advertiser_bare_zeroconf_and_none():
    adv, created = _make([])
    adv.start()
    mdns.stop_mdns(adv)
    assert created[0].closed
    assert adv.zc is None

    bare = FakeZc()
    mdns.stop_mdns(bare)
    assert bare.unregistered and bare.closed

    mdns.stop_mdns(None)  # no-op


def test_watchdog_thread_ticks_and_stops():
    adv, created = _make([False, False, True, True, True, True, True, True])
    adv.interval = 0.05
    adv.start()
    deadline = time.monotonic() + 5
    while adv.rebuilds == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    adv.close()
    assert adv.rebuilds == 1
    assert len(created) == 2
    assert all(zc.closed for zc in created)
    assert adv._thread is not None
    assert not adv._thread.is_alive()


def test_encode_mdns_query_is_a_single_a_question():
    pkt = mdns._encode_mdns_query("hokku.local", 0xBEEF)
    txid, flags, qd, an, ns, ar = struct.unpack(">HHHHHH", pkt[:12])
    assert (txid, flags, qd, an, ns, ar) == (0xBEEF, 0, 1, 0, 0, 0)
    assert pkt[12:] == b"\x05hokku\x05local\x00" + struct.pack(">HH", 1, 1)


def test_probe_returns_false_when_nothing_answers():
    # Loopback-bound: a fresh transaction id nobody is listening for.
    assert mdns.probe_mdns("no-such-host-xyz", "127.0.0.1", timeout=0.3) is False


def test_probe_returns_false_on_unbindable_ip():
    assert mdns.probe_mdns("frame", "203.0.113.7", timeout=0.3) is False


@pytest.mark.skipif(mdns._get_local_ip().startswith("127."), reason="no LAN interface")
def test_probe_answers_live_responder():
    """Real Zeroconf, real multicast query, real unicast reply."""
    hostname = f"hokkutest-{int(time.time()) % 100000}"
    adv = mdns.start_mdns(18099, hostname)
    try:
        assert adv.zc is not None, "registration failed"
        ip = adv.local_ip
        assert ip is not None
        # Legacy-unicast reply must carry our A record.
        answered = any(mdns.probe_mdns(hostname, ip) for _ in range(3))
        assert answered, "healthy responder did not answer a multicast query"
        assert mdns.probe_mdns("definitely-not-" + hostname, ip, timeout=0.5) is False
    finally:
        mdns.stop_mdns(adv)


def test_probe_ignores_unrelated_datagrams(monkeypatch):
    """A reply with a foreign transaction id must not count as an answer."""
    calls = {"n": 0}

    class FakeSock:
        def __init__(self, *a, **k): pass
        def settimeout(self, t): pass
        def bind(self, addr): pass
        def setsockopt(self, *a): pass
        def sendto(self, data, addr): calls["sent"] = data
        def close(self): pass
        def recvfrom(self, n):
            calls["n"] += 1
            if calls["n"] == 1:
                return struct.pack(">HHHHHH", 0x0001, 0x8400, 0, 1, 0, 0), ("10.0.0.1", 5353)
            raise socket.timeout()

    monkeypatch.setattr(mdns.socket, "socket", FakeSock)
    assert mdns.probe_mdns("frame", "10.0.0.5", timeout=0.2) is False
    assert calls["n"] == 2
