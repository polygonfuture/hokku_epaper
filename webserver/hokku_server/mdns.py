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
import time
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


def start_mdns(port: int, hostname: str) -> Any:
    """Register the Hokku HTTP service via mDNS.

    Advertises as ``Hokku._http._tcp.local.`` with A-record
    ``<hostname>.local.`` pointing to the local LAN IP.

    *hostname* is the label before ``.local`` (e.g. ``"hokku-server"``).
    Pass an empty string to skip registration (caller's responsibility).

    Returns the ``Zeroconf`` instance (keep the reference alive for the life
    of the process).  Logs and returns ``None`` on unexpected failure.
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
    # network never leaves the server with no mDNS at all. local_ip is recomputed each
    # start, so a new DHCP lease is picked up on restart (pair with a DHCP reservation to
    # pin the IP permanently — zeroconf does not refresh a live A record on IP change).
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
            return zc
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


def stop_mdns(zc: Any) -> None:
    """Unregister all services and close the Zeroconf instance.

    Safe to call with ``None`` (no-op).
    """
    if zc is None:
        return
    try:
        zc.unregister_all_services()
        zc.close()
        logger.info("Advertisement stopped")
    except Exception as exc:
        logger.warning("Error while stopping — %s", exc)
