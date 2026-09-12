"""Bonjour / mDNS advertisement for the Hokku HTTP service.

Advertises ``_http._tcp.local.`` so that browsers and devices on the LAN
can reach the server at ``<hostname>.local`` without knowing its IP.
The service appears as ``Hokku <hostname>._http._tcp.local.`` with a
``path=/hokku/ui`` TXT record. Using the hostname in the instance name
keeps multiple hokku servers on the same LAN from colliding during probing.

────────────────────────────────────────────────────────────────────────────
RUNNING WITH A VPN (OpenVPN, WireGuard, Tailscale, a commercial VPN client, …)?
────────────────────────────────────────────────────────────────────────────
mDNS is made VPN-safe here in two ways, so ``hokku.local`` keeps working even
while a VPN is connected on the machine hosting the server:

  1. The advertised A record uses the real LAN IP, never the VPN tunnel IP —
     ``_get_local_ip()`` scores interfaces and skips VPN/virtual adapters
     (see ``_VIRTUAL_IFACE``).
  2. Zeroconf is bound to that LAN IP ONLY (see ``start_mdns``). A default
     ``Zeroconf()`` binds ALL interfaces, so its mDNS *responses* can egress the
     VPN interface (VPN clients commonly give their adapter a lower interface
     metric than Wi-Fi) and never reach LAN clients — the e-ink frame then can't
     resolve ``hokku.local`` even though the record is correct.
  3. A watchdog thread re-checks the advertisement every ``PROBE_INTERVAL``
     seconds by sending a real mDNS query for ``<hostname>.local`` to the
     multicast group from the LAN IP. Windows silently drops a socket's
     224.0.0.251 group membership when the Wi-Fi profile or a VPN adapter
     bounces (a VPN client rebuilding its WinTUN adapter, for one); the responder then
     still answers unicast but is deaf to every multicast query on the LAN, and
     the frame gets "Image download failed" until the server restarts. Two
     unanswered probes in a row, or a changed LAN IP, tear the Zeroconf instance
     down and re-register it — the same self-heal lwIP does for an ESP32.

If ``hokku.local`` still won't resolve while a VPN is up, the culprit is
usually the CLIENT machine (e.g. your PC/phone), not this server: the VPN's
interface has a lower metric, so the OS sends the ``.local`` query to the VPN's
tunnel DNS first, which returns NXDOMAIN and the OS caches that failure. Fixes,
in order of robustness: (a) a hosts-file / DNS entry pinning ``hokku.local`` to
the server's LAN IP (bypasses DNS/VPN entirely); (b) enable "Allow LAN Traffic"
in the VPN client; (c) ``ipconfig /flushdns`` after the VPN connects to clear
the stale negative cache. Pair a DHCP reservation with (a) so the IP never
drifts — zeroconf does not refresh a live A record when the IP changes.
"""

from __future__ import annotations

import logging
import re
import socket
import struct
import threading
import time
from collections.abc import Callable
from typing import Any

import psutil
from zeroconf import InterfaceChoice, ServiceInfo, Zeroconf

logger = logging.getLogger(__name__)

# Interface-name fragments that indicate a VPN or virtual adapter. Their
# addresses are usually unreachable by other devices on the LAN, so advertising
# one as <host>.local would make the name resolve to a dead address.
_VIRTUAL_IFACE = re.compile(
    r"vethernet|hyper-?v|wsl|virtualbox|vmware|vmnet|loopback|tap-|tun\d|wintun|"
    r"tailscale|zerotier|wireguard|openvpn|pia|npcap|bluetooth|docker",
    re.IGNORECASE,
)


def _default_route_ip() -> str | None:
    """The source IP the OS would use to reach the internet. No packets sent."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return None


def _is_rfc1918(ip: str) -> bool:
    return ip.startswith(("10.", "192.168.")) or bool(re.match(r"172\.(1[6-9]|2\d|3[01])\.", ip))


def _get_local_ip() -> str:
    """Return the best LAN IPv4 to advertise via mDNS.

    Prefers a real, up, private (RFC1918) interface over VPN/virtual adapters.
    A VPN address (e.g. a 10.x tunnel that owns the default route) is one
    other LAN devices — including the e-paper frame — cannot reach, so we must
    not advertise it as ``<host>.local``. Falls back to the default-route
    address, then loopback.
    """
    route_ip = _default_route_ip()
    best: str | None = None
    best_score = -1
    try:
        stats = psutil.net_if_stats()
        for iface, addrs in psutil.net_if_addrs().items():
            st = stats.get(iface)
            if st is not None and not st.isup:
                continue
            for a in addrs:
                if a.family != socket.AF_INET:
                    continue
                ip = a.address
                if ip.startswith(("127.", "169.254.")):
                    continue
                virtual = bool(_VIRTUAL_IFACE.search(iface))
                score = (0 if virtual else 10) + (2 if _is_rfc1918(ip) else 0)
                if not virtual and ip == route_ip:
                    score += 1
                if score > best_score:
                    best_score, best = score, ip
    except Exception as e:
        logger.debug("interface scan failed, falling back to default route: %s", e)

    return best or route_ip or "127.0.0.1"


# Watchdog cadence. 30 s means the LAN is without hokku.local for at most
# ~2 probes + 1 rebuild (≈ 65 s) after a membership loss; the frame retries a
# failed download every 60 s, so it recovers on its first or second retry.
PROBE_INTERVAL = 30.0
PROBE_TIMEOUT = 1.5
PROBE_ATTEMPTS = 2  # queries per tick before the tick counts as a miss
PROBE_MISSES_BEFORE_REBUILD = 2  # consecutive missed ticks

_MDNS_GROUP = "224.0.0.251"
_MDNS_PORT = 5353
_TYPE_A = 1
_CLASS_IN = 1


def _encode_mdns_query(name: str, txid: int) -> bytes:
    """A single-question A-record query for *name* (e.g. ``hokku.local``)."""
    labels = b"".join(bytes([len(label)]) + label.encode("ascii") for label in name.split("."))
    header = struct.pack(">HHHHHH", txid & 0xFFFF, 0, 1, 0, 0, 0)
    return header + labels + b"\x00" + struct.pack(">HH", _TYPE_A, _CLASS_IN)


def probe_mdns(hostname: str, local_ip: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """Return True if a responder on the LAN answers a multicast query for ``<hostname>.local``.

    The query is sent to the mDNS group from an ephemeral port on *local_ip*, so per
    RFC 6762 §6.7 it is a legacy-unicast query: the responder must have RECEIVED it
    via the multicast group (the path Windows silently breaks) but replies unicast to
    our port, which needs no group membership on the probing socket. Each call uses a
    fresh transaction id and port so zeroconf's duplicate-question suppression never
    swallows a repeat probe.
    """
    name = f"{hostname}.local"
    txid = int(time.monotonic() * 1000) & 0xFFFF or 1
    packet = _encode_mdns_query(name, txid)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    except OSError:
        return False
    try:
        sock.bind((local_ip, 0))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
        sock.sendto(packet, (_MDNS_GROUP, _MDNS_PORT))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            sock.settimeout(remaining)
            data, _addr = sock.recvfrom(1500)
            # Accept only an answer to *our* question: matching id, QR bit set, >=1 answer.
            if len(data) >= 12:
                rid, flags, _qd, ancount = struct.unpack(">HHHH", data[:8])
                if rid == txid and flags & 0x8000 and ancount > 0:
                    return True
    except OSError:
        return False
    finally:
        sock.close()


def _register(port: int, hostname: str) -> tuple[Any, str] | None:
    """Create a Zeroconf bound to the LAN IP and register the service.

    Returns ``(zeroconf, local_ip)`` or ``None`` after three failed attempts.
    """
    local_ip = _get_local_ip()
    # Service instance name is unique per hostname so multiple hokku servers
    # on the same LAN don't collide during mDNS probing.
    # The A record (server=) is what drives <hostname>.local resolution.
    instance = f"Hokku {hostname}._http._tcp.local."
    info = ServiceInfo(
        "_http._tcp.local.",
        instance,
        addresses=[socket.inet_aton(local_ip)],
        port=port,
        properties={"path": "/hokku/ui"},
        server=f"{hostname}.local.",
    )
    # Bind Zeroconf to the chosen LAN IP ONLY — never every interface. A default
    # Zeroconf() binds to ALL interfaces including a VPN tunnel (which often gets a
    # lower interface metric than Wi-Fi), so its mDNS responses can egress the VPN and
    # never reach LAN clients — the frame then can't resolve hokku.local while the VPN is
    # up, even though the A record is correct. Pinning to local_ip (which _get_local_ip
    # already picks, skipping VPN/virtual adapters) keeps announcements on the LAN. Falls
    # back to Default (default-route iface) then All if the pin is rejected, so a weird
    # network never leaves the server with no mDNS at all. local_ip is recomputed on
    # every (re)registration, so a new DHCP lease is picked up by the watchdog rebuild.
    bind_choices = [[local_ip], InterfaceChoice.Default, InterfaceChoice.All]
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        zc = None
        binding = bind_choices[min(attempt - 1, len(bind_choices) - 1)]
        try:
            zc = Zeroconf(interfaces=binding)
            zc.register_service(info)
            logger.info(
                "Advertised as %s.local (%s:%s) bound to %s",
                hostname, local_ip, port,
                binding if isinstance(binding, list) else binding.name,
            )
            return zc, local_ip
        except Exception as exc:
            last_exc = exc
            # Always close on failure — an unclosed Zeroconf instance keeps
            # background threads alive and will respond to future probes as if
            # it owns the name, causing NonUniqueNameException on every retry.
            if zc is not None:
                try:
                    zc.close()
                except Exception as e:
                    logger.warning("zeroconf close failed: %s", e)
            if attempt < 3:
                logger.warning(
                    "Attempt %d failed (%s: %s) — retrying in 3s",
                    attempt,
                    type(exc).__name__,
                    exc,
                )
                time.sleep(3)

    logger.error("Registration failed — %s: %s", type(last_exc).__name__, last_exc)
    return None


def _close_zeroconf(zc: Any) -> None:
    if zc is None:
        return
    try:
        zc.unregister_all_services()
        zc.close()
    except Exception as exc:
        logger.warning("Error while stopping — %s", exc)


def _default_probe(hostname: str, local_ip: str) -> bool:
    return any(probe_mdns(hostname, local_ip) for _ in range(PROBE_ATTEMPTS))


class MdnsAdvertiser:
    """A self-healing ``<hostname>.local`` advertisement.

    Owns the live ``Zeroconf`` instance and a daemon watchdog thread that keeps
    proving, with a real multicast query, that the LAN can still reach it. When
    ``PROBE_MISSES_BEFORE_REBUILD`` consecutive ticks go unanswered, or the LAN IP
    changes, the instance is torn down and re-registered from scratch.

    ``register`` / ``probe`` / ``get_ip`` are injectable for tests; production uses
    the module-level functions.
    """

    def __init__(
        self,
        port: int,
        hostname: str,
        *,
        interval: float = PROBE_INTERVAL,
        register: Callable[[int, str], tuple[Any, str] | None] = _register,
        probe: Callable[[str, str], bool] = _default_probe,
        get_ip: Callable[[], str] = _get_local_ip,
    ) -> None:
        self.port = port
        self.hostname = hostname
        self.interval = interval
        self._register_fn = register
        self._probe_fn = probe
        self._get_ip = get_ip
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.zc: Any = None
        self.local_ip: str | None = None
        self.rebuilds = 0
        self.misses = 0
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        """Register and start the watchdog.

        Returns False if registration failed (the watchdog still runs and keeps
        retrying every tick).
        """
        ok = self._register_locked()
        self._thread = threading.Thread(target=self._run, name="mdns-watchdog", daemon=True)
        self._thread.start()
        return ok

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=PROBE_TIMEOUT * PROBE_ATTEMPTS + 5)
        with self._lock:
            old, self.zc = self.zc, None
        _close_zeroconf(old)
        logger.info("Advertisement stopped")

    # -- internals ---------------------------------------------------------

    def _register_locked(self) -> bool:
        with self._lock:
            result = self._register_fn(self.port, self.hostname)
            if result is None:
                self.zc, self.local_ip = None, None
                return False
            self.zc, self.local_ip = result
            self.misses = 0
            return True

    def _rebuild(self, reason: str) -> None:
        with self._lock:
            old, self.zc = self.zc, None
        _close_zeroconf(old)
        self.rebuilds += 1
        logger.warning(
            "mDNS watchdog: %s — re-registering %s.local (rebuild #%d)",
            reason, self.hostname, self.rebuilds,
        )
        if not self._register_locked():
            logger.error("mDNS watchdog: re-registration failed; will retry in %.0fs", self.interval)

    def check_once(self) -> str | None:
        """One watchdog tick. Returns the rebuild reason if a rebuild happened, else None."""
        with self._lock:
            zc, ip = self.zc, self.local_ip
        if zc is None or ip is None:
            reason = "no live advertisement"
            self._rebuild(reason)
            return reason
        current_ip = self._get_ip()
        if current_ip != ip:
            reason = f"LAN IP changed {ip} -> {current_ip}"
            self._rebuild(reason)
            return reason
        if self._probe_fn(self.hostname, ip):
            if self.misses:
                logger.info("mDNS watchdog: %s.local answering again", self.hostname)
            self.misses = 0
            return None
        self.misses += 1
        logger.warning(
            "mDNS watchdog: no answer to multicast query for %s.local (%d/%d)",
            self.hostname, self.misses, PROBE_MISSES_BEFORE_REBUILD,
        )
        if self.misses >= PROBE_MISSES_BEFORE_REBUILD:
            reason = f"{self.misses} unanswered multicast probes (group membership lost?)"
            self._rebuild(reason)
            return reason
        return None

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.check_once()
            except Exception as exc:  # never let the watchdog die
                logger.warning("mDNS watchdog tick failed: %s", exc)


def start_mdns(port: int, hostname: str) -> MdnsAdvertiser:
    """Register the Hokku HTTP service via mDNS and keep it alive.

    Advertises as ``Hokku <hostname>._http._tcp.local.`` with A-record
    ``<hostname>.local.`` pointing to the local LAN IP, then watches it
    (see ``MdnsAdvertiser``).

    *hostname* is the label before ``.local`` (e.g. ``"hokku-server"``).
    Pass an empty string to skip registration (caller's responsibility).

    Returns the ``MdnsAdvertiser`` (keep the reference alive for the life of
    the process and hand it to ``stop_mdns``). The advertiser is returned even if
    the first registration failed — its watchdog keeps retrying every
    ``PROBE_INTERVAL`` seconds instead of leaving the server with no mDNS.
    """
    adv = MdnsAdvertiser(port, hostname)
    adv.start()
    return adv


def stop_mdns(zc: Any) -> None:
    """Stop an advertiser returned by ``start_mdns`` (or a bare ``Zeroconf``).

    Safe to call with ``None`` (no-op).
    """
    if zc is None:
        return
    if isinstance(zc, MdnsAdvertiser):
        zc.close()
        return
    _close_zeroconf(zc)
    logger.info("Advertisement stopped")
