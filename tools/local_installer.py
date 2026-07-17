"""Install and run the hokku image server on this PC.

The Raspberry Pi path (pi_installer) images an SD card; this is the alternative
for people who want the server on a machine they already own — a desktop, a
laptop, a NAS. It:

  - creates an install directory with images/ + cache/ + config.json,
  - registers a Windows Scheduled Task so the server starts at boot,
  - opens the firewall for the HTTP port and mDNS,
  - starts the server now (no reboot needed) and waits for it to answer,
  - works out which address to hand the frame — the mDNS name (hokku.local)
    if it resolves back to this PC, else the detected LAN IP.

The server itself is cross-platform, but the autostart + firewall steps are
Windows-specific (Scheduled Tasks / netsh). On Linux/macOS the Debian package
or a hand-written systemd unit is the better answer; run() says so and stops.

run() returns a dict shaped like pi_installer.run() so the wizard can feed the
ESP32 phase the same way for either install path.
"""

from __future__ import annotations

import ctypes
import json
import logging
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import release_cache
from setup_prompts import (
    _prompt_with_sticky,
    _yesno,
    validate_mdns_hostname,
    validate_ssid,
    validate_wifi_password,
)

logger = logging.getLogger(__name__)

REPO_ROOT = release_cache.REPO_ROOT
WEBSERVER_DIR = REPO_ROOT / "webserver"
VENV_PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
STATE_FILE = release_cache.CACHE_DIR / "local_install.json"

TASK_NAME = "HokkuServer"
DEFAULT_PORT = 8080
DEFAULT_MDNS_HOSTNAME = "hokku"
MDNS_PORT = 5353

# Fallback config schema version if we cannot read the real one from the server
# package. Kept in sync with webserver/hokku_server/app_config.py _CURRENT_VERSION;
# test_local_installer asserts they match.
_FALLBACK_CONFIG_VERSION = 7

# Interface-name fragments that indicate a virtual/VPN adapter whose address is
# almost never the one the frame should use to reach this PC.
_VIRTUAL_IFACE = re.compile(
    r"vethernet|hyper-?v|wsl|virtualbox|vmware|vmnet|loopback|tap-|tun\d|wintun|"
    r"tailscale|zerotier|wireguard|openvpn|npcap|bluetooth|docker",
    re.IGNORECASE,
)


# ---------- small process wrapper (monkeypatched in tests) ----------


def _run(argv, **kwargs):
    """Run a command, returning the CompletedProcess. Thin wrapper so tests can
    intercept every schtasks/netsh/powercfg invocation."""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(argv, **kwargs)


def _is_admin():
    """True if the current process has Administrator rights (Windows), else False.
    Non-Windows always returns False — the privileged steps don't run there."""
    if sys.platform != "win32":
        return False
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


# ---------- install state / idempotency ----------


def load_state():
    """Return the saved local-install state dict, or {} if none."""
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state):
    """Persist the local-install state (task name, port, firewall rule names…)."""
    release_cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def task_exists(name=TASK_NAME):
    """True if a Scheduled Task with *name* is registered."""
    if sys.platform != "win32":
        return False
    result = _run(["schtasks", "/Query", "/TN", name])
    return result.returncode == 0


def is_installed():
    """True if we have saved state and its Scheduled Task still exists."""
    return bool(load_state()) and task_exists(load_state().get("task_name", TASK_NAME))


# ---------- prereqs ----------


def venv_python():
    """Return the venv interpreter path, or None if the venv isn't built yet."""
    return VENV_PYTHON if VENV_PYTHON.exists() else None


def ensure_requirements(python_exe):
    """Install requirements.txt into the venv so the server can run. Streams pip
    output (heavy deps: numpy/numba/opencv). Returns True on success."""
    req = REPO_ROOT / "requirements.txt"
    print(f"  Installing server dependencies from {req.name} (this can take a few minutes)...")
    result = _run(
        [str(python_exe), "-m", "pip", "install", "-r", str(req)],
        capture_output=False,
    )
    if result.returncode != 0:
        print("  ERROR: dependency install failed. Fix the errors above and re-run.")
        return False
    return True


# ---------- install dir + server config ----------


def _is_fixed_drive(root):
    """True if *root* is a fixed (non-removable, non-network) drive on Windows."""
    if sys.platform != "win32":
        return True
    try:
        DRIVE_FIXED = 3
        return ctypes.windll.kernel32.GetDriveTypeW(str(root)) == DRIVE_FIXED
    except Exception:
        return True


def default_install_dir():
    """A visible default install dir. Prefers a fixed data drive (D:, E:) so the
    SYSTEM-run task can always see it at boot; falls back to the home dir."""
    if sys.platform == "win32":
        for letter in ("D", "E", "C"):
            root = Path(f"{letter}:/")
            if root.exists() and _is_fixed_drive(root):
                return root / "Hokku"
        return Path.home() / "Hokku"
    return Path.home() / "hokku"


def prompt_install_dir(default):
    """Ask where to install; return a Path. Empty input keeps *default*."""
    print(f"  Where should photos and server data live? [{default}]")
    val = input("  Install directory: ").strip()
    return Path(val) if val else Path(default)


def create_dirs(install_dir):
    """Create <install_dir>/images, /cache, /logs. Returns (images, cache, logs)."""
    images = install_dir / "images"
    cache = install_dir / "cache"
    logs = install_dir / "logs"
    for d in (images, cache, logs):
        d.mkdir(parents=True, exist_ok=True)
    return images, cache, logs


def _current_config_version():
    """The server's current config schema version. Reads it from the source of
    truth (app_config.py) without importing the heavy server package."""
    src = WEBSERVER_DIR / "hokku_server" / "app_config.py"
    try:
        text = src.read_text(encoding="utf-8")
        m = re.search(r"^_CURRENT_VERSION\s*=\s*(\d+)", text, re.MULTILINE)
        if m:
            return int(m.group(1))
    except OSError:
        pass
    return _FALLBACK_CONFIG_VERSION


def write_server_config(install_dir, images_dir, cache_dir, port, mdns_hostname):
    """Write config.json. The `version` key is mandatory — without it the server
    discards every other key and falls back to POSIX defaults, then exits because
    /var/lib/hokku doesn't exist. Missing keys are filled by AppConfig defaults
    at load time, so a minimal-but-versioned file is complete."""
    config = {
        "version": _current_config_version(),
        "upload_dir": images_dir.as_posix(),
        "cache_dir": cache_dir.as_posix(),
        "port": port,
        "mdns_hostname": mdns_hostname,
    }
    path = install_dir / "config.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


# ---------- Scheduled Task (Windows autostart) ----------


def _xml_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def build_task_xml(python_exe, config_path, workdir, log_path):
    """Return the Task Scheduler XML (as UTF-16 bytes) for the server task.

    Runs as SYSTEM (starts at boot, no login, no stored password), one minute
    after boot (mDNS/DHCP settle), restarts on failure, never times out. The
    action wraps python in cmd.exe so stderr (the server's log stream) is
    redirected to a file — the Windows stand-in for journalctl.
    """
    inner = f'"{python_exe}" -m hokku_server "{config_path}" >> "{log_path}" 2>&1'
    arguments = _xml_escape(f'/c "{inner}"')
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Hokku e-paper image server</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
      <Delay>PT1M</Delay>
    </BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>cmd.exe</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{_xml_escape(str(workdir))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""
    return xml.encode("utf-16")


def register_scheduled_task(python_exe, config_path, workdir, log_path, name=TASK_NAME):
    """Create (or replace, via /F) the boot Scheduled Task. Returns True on success."""
    release_cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    xml_path = release_cache.CACHE_DIR / "hokku_task.xml"
    xml_path.write_bytes(build_task_xml(python_exe, config_path, workdir, log_path))
    result = _run(
        ["schtasks", "/Create", "/TN", name, "/XML", str(xml_path), "/F", "/RU", "SYSTEM"]
    )
    if result.returncode != 0:
        print(f"  ERROR: could not register the Scheduled Task: {result.stderr.strip()}")
        return False
    return True


def start_task(name=TASK_NAME):
    """Start the task now so the user doesn't have to reboot. Returns True on success."""
    result = _run(["schtasks", "/Run", "/TN", name])
    return result.returncode == 0


def unregister_scheduled_task(name=TASK_NAME):
    """Delete the Scheduled Task. Returns True if it's gone (or was never there)."""
    if not task_exists(name):
        return True
    result = _run(["schtasks", "/Delete", "/TN", name, "/F"])
    return result.returncode == 0


# ---------- firewall ----------


def firewall_rule_names(port):
    """Return (tcp_rule_name, mdns_rule_name) for the given port."""
    return (f"Hokku Server (TCP {port})", f"Hokku Server mDNS (UDP {MDNS_PORT})")


def add_firewall_rules(port, program):
    """Add inbound allow rules for the HTTP port and mDNS. Deletes any same-named
    rule first so re-runs don't stack duplicates. Returns the rule names added."""
    tcp_name, mdns_name = firewall_rule_names(port)
    specs = [
        (tcp_name, "TCP", str(port)),
        (mdns_name, "UDP", str(MDNS_PORT)),
    ]
    for name, proto, localport in specs:
        _run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"])
        _run(
            [
                "netsh",
                "advfirewall",
                "firewall",
                "add",
                "rule",
                f"name={name}",
                "dir=in",
                "action=allow",
                f"protocol={proto}",
                f"localport={localport}",
                "profile=private,domain",
                f"program={program}",
                "enable=yes",
            ]
        )
    return [tcp_name, mdns_name]


def remove_firewall_rules(rule_names):
    """Delete the named firewall rules (ignoring 'not found')."""
    for name in rule_names:
        _run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"])


def network_is_public():
    """True if any active network profile is categorised Public — in which case
    the private/domain firewall rules won't apply and the frame can't connect.
    Returns None if we can't tell."""
    if sys.platform != "win32":
        return None
    result = _run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "(Get-NetConnectionProfile).NetworkCategory -join ','",
        ]
    )
    if result.returncode != 0:
        return None
    return "Public" in (result.stdout or "")


# ---------- health check ----------


def wait_for_server(port, timeout=90):
    """Poll http://127.0.0.1:<port>/hokku/api/time until it answers. True if up."""
    url = f"http://127.0.0.1:{port}/hokku/api/time"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=3)
            return True
        except urllib.error.HTTPError:
            return True  # any HTTP response means the server is serving
        except OSError:
            time.sleep(2)
    return False


# ---------- LAN IP detection ----------


def _default_route_ip():
    """The source IP the OS would use to reach the internet. No packets sent."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return None


def _is_rfc1918(ip):
    return ip.startswith(("10.", "192.168.")) or bool(re.match(r"172\.(1[6-9]|2\d|3[01])\.", ip))


def _rank_candidates(interfaces, route_ip):
    """Pure ranking: *interfaces* is a list of (iface_name, ipv4). Returns a
    scored, sorted list of dicts. Virtual/VPN adapters and non-LAN addresses
    sink to the bottom; the default-route address floats to the top."""
    cands = []
    for iface, ip in interfaces:
        if not ip or ip.startswith(("127.", "169.254.")):
            continue
        virtual = bool(_VIRTUAL_IFACE.search(iface))
        score = 0 if virtual else 10
        if ip == route_ip:
            score += 5
        if _is_rfc1918(ip):
            score += 2
        cands.append({"ip": ip, "iface": iface, "virtual": virtual, "score": score})
    cands.sort(key=lambda c: -c["score"])
    return cands


def detect_lan_ip():
    """Return (best_ip, candidates). Uses psutil if available for a full
    interface list; falls back to the default-route address alone."""
    route_ip = _default_route_ip()
    interfaces = []
    try:
        import psutil  # noqa: PLC0415 — optional dep; fall back to the route IP without it

        stats = psutil.net_if_stats()
        for iface, addrs in psutil.net_if_addrs().items():
            st = stats.get(iface)
            if st is not None and not st.isup:
                continue
            for a in addrs:
                if a.family == socket.AF_INET:
                    interfaces.append((iface, a.address))
    except Exception:
        if route_ip:
            interfaces.append(("default-route", route_ip))

    cands = _rank_candidates(interfaces, route_ip)
    best = cands[0]["ip"] if cands else route_ip
    return best, cands


def prompt_lan_ip(best, candidates):
    """Confirm the LAN IP. Auto-accepts when there's a single non-virtual choice;
    otherwise lists candidates and asks."""
    real = [c for c in candidates if not c["virtual"]]
    if len(real) <= 1:
        return best
    print()
    print("  This PC's address on the LAN")
    print("  ----------------------------")
    for i, c in enumerate(candidates, 1):
        tag = "  (virtual — probably not this)" if c["virtual"] else ""
        default = "  <-- default" if c["ip"] == best else ""
        print(f"    [{i}] {c['ip']:<16} {c['iface']}{tag}{default}")
    val = input(f"  Which address will the frame reach this PC on? [{best}]: ").strip()
    if not val:
        return best
    if val.isdigit() and 1 <= int(val) <= len(candidates):
        return candidates[int(val) - 1]["ip"]
    return val


# ---------- address the frame should use ----------


def _own_ipv4_addresses():
    """Every IPv4 address bound to this machine, across all adapters (including
    VPNs). Used to tell 'this PC on another interface' apart from a genuinely
    different host when a .local name resolves unexpectedly."""
    ips = set()
    try:
        import psutil  # noqa: PLC0415 — optional; socket fallback below

        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family == socket.AF_INET:
                    ips.add(a.address)
    except Exception:
        try:
            _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
            ips.update(addrs)
        except OSError:
            pass
    return ips


def resolve_server_host(mdns_hostname, port, lan_ip):
    """Decide the host string burned into the frame's image_url.

    Prefer <mdns_hostname>.local when it resolves back to THIS PC's LAN address
    (survives DHCP lease changes). Fall back to the LAN IP when mDNS doesn't
    resolve, when the name is advertised on a different local adapter (e.g. a
    VPN the frame can't reach), or when a genuinely different host owns it.
    """
    if not mdns_hostname:
        return lan_ip
    # Imported lazily: esp32_setup pulls in esptool, and this module must stay
    # importable (for tests) without it.
    import esp32_setup  # noqa: PLC0415

    fqdn = f"{mdns_hostname}.local"
    reachable, resolved = esp32_setup._check_server_reachable(fqdn, port)
    if reachable and (resolved is None or resolved == lan_ip):
        print(f"  {fqdn} resolves to this PC and answers HTTP — using {fqdn}.")
        return fqdn
    if reachable and resolved and resolved != lan_ip:
        if resolved in _own_ipv4_addresses():
            print(f"  {fqdn} is advertised on a different adapter of this PC ({resolved}),")
            print("  most likely a VPN the frame can't reach. Pointing the frame at the")
            print(f"  LAN IP {lan_ip} instead. (Disconnect the VPN and re-run if you'd")
            print(f"  rather use {fqdn}.)")
        else:
            print(f"  WARNING: {fqdn} resolves to {resolved}, a different machine — not")
            print(f"  this PC ({lan_ip}). Using the LAN IP instead.")
        return lan_ip
    print(f"  {fqdn} did not resolve from this PC — using {lan_ip}.")
    return lan_ip


# ---------- sleep warning ----------


def warn_if_pc_sleeps():
    """Warn (and offer to fix) if the PC will sleep on AC — a sleeping server is
    unreachable when the frame wakes to fetch its next image."""
    if sys.platform != "win32":
        return
    result = _run(["powercfg", "/query", "SCHEME_CURRENT", "SUB_SLEEP", "STANDBYIDLE"])
    out = result.stdout or ""
    # "Current AC Power Setting Index: 0x00000000" means never sleep on AC.
    m = re.search(r"Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)", out)
    sleeps = bool(m) and int(m.group(1), 16) != 0
    if not sleeps:
        return
    print()
    print("  WARNING: this PC is set to sleep on AC power. While it sleeps the")
    print("  server is unreachable, so the frame will show an error when it wakes")
    print("  to refresh. An always-on server should never sleep.")
    if _yesno("  Disable sleep-on-AC now?", default_yes=True):
        _run(["powercfg", "/change", "standby-timeout-ac", "0"])
        print("  Sleep-on-AC disabled.")


# ---------- orchestration ----------


def _collect_frame_wifi(sticky):
    """Prompt for the WiFi the FRAME will join (the PC may be on Ethernet).
    Returns (ssid, password), pre-filled from sticky settings."""
    print()
    print("  The frame needs WiFi credentials to reach this server.")
    ssid = _prompt_with_sticky("Frame WiFi SSID", validate_ssid, sticky.get("wifi_ssid"), None)
    password = _prompt_with_sticky(
        "Frame WiFi password",
        validate_wifi_password,
        sticky.get("wifi_pass"),
        None,
        hidden=True,
    )
    return ssid, password


def run():
    """Full local-PC install. Returns a pi_installer.run()-compatible dict, or
    None if the install did not complete."""
    print()
    print("  Install the hokku server on this PC")
    print("  -----------------------------------")

    if sys.platform != "win32":
        print("  Automated local install is currently Windows-only.")
        print("  On Linux/macOS install the .deb or run the server from source and")
        print("  set up a systemd unit — see docs/install.md section 1.1.")
        return None

    if not _is_admin():
        print("  ERROR: administrator rights are required (Scheduled Task + firewall).")
        print("  Re-launch via hokku_setup.bat, which elevates automatically.")
        return None

    python_exe = venv_python()
    if python_exe is None:
        print(f"  ERROR: no virtual environment at {VENV_PYTHON}.")
        print("  Run hokku_setup.bat (it builds the venv) or create it manually.")
        return None

    if not ensure_requirements(python_exe):
        return None

    sticky = release_cache.load_settings()

    # Install dir + config.
    install_dir = prompt_install_dir(sticky.get("local_install_dir") or default_install_dir())
    if not _is_fixed_drive(Path(f"{install_dir.drive}/") if install_dir.drive else install_dir):
        print(f"  WARNING: {install_dir.drive} is not a fixed drive. The boot task runs")
        print("  as SYSTEM and may not see removable or network drives at startup.")
        if not _yesno("  Use it anyway?", default_yes=False):
            return None

    port = int(sticky.get("local_port") or DEFAULT_PORT)
    mdns_hostname = _prompt_with_sticky(
        "Server mDNS hostname (advertises as <name>.local)",
        validate_mdns_hostname,
        sticky.get("mdns_hostname"),
        DEFAULT_MDNS_HOSTNAME,
    )

    images_dir, cache_dir, logs_dir = create_dirs(install_dir)
    config_path = write_server_config(install_dir, images_dir, cache_dir, port, mdns_hostname)
    log_path = logs_dir / "server.log"
    print(f"  Wrote {config_path}")

    # Firewall + autostart.
    rule_names = add_firewall_rules(port, python_exe)
    if network_is_public():
        print("  WARNING: an active network is categorised 'Public'. The firewall")
        print("  rules apply to Private/Domain networks only, so the frame may be")
        print("  blocked. Set the network to Private in Windows settings.")

    if not register_scheduled_task(python_exe, config_path, WEBSERVER_DIR, log_path):
        return None
    start_task()
    print("  Registered and started the boot service.")

    # Health check.
    if wait_for_server(port):
        print(f"  Server is up at http://127.0.0.1:{port}/hokku/ui")
        webserver_ok = True
    else:
        print(f"  Server did not answer within the timeout. Check {log_path}.")
        webserver_ok = False

    warn_if_pc_sleeps()

    # Which address the frame should use.
    lan_ip, candidates = detect_lan_ip()
    lan_ip = prompt_lan_ip(lan_ip, candidates)
    server_host = resolve_server_host(mdns_hostname, port, lan_ip)

    # WiFi for the frame.
    ssid, password = _collect_frame_wifi(sticky)

    # Persist sticky + state.
    sticky.update(
        {
            "wifi_ssid": ssid,
            "wifi_pass": password,
            "mdns_hostname": mdns_hostname,
            "local_install_dir": str(install_dir),
            "local_port": port,
        }
    )
    release_cache.save_settings(sticky)
    save_state(
        {
            "task_name": TASK_NAME,
            "install_dir": str(install_dir),
            "config_path": str(config_path),
            "port": port,
            "mdns_hostname": mdns_hostname,
            "firewall_rules": rule_names,
            "server_host": server_host,
        }
    )

    print()
    print(f"  Done. The frame will be pointed at {server_host}:{port}.")
    return {
        "wifi_ssid": ssid,
        "wifi_pass": password,
        "server_ip": server_host,
        "hostname": mdns_hostname,
        "webserver_ok": webserver_ok,
    }


def uninstall():
    """Remove the Scheduled Task and firewall rules. Leaves the install dir (and
    the user's photos) untouched. Returns 0 on success."""
    print()
    print("  Uninstall local hokku server")
    print("  ----------------------------")
    if sys.platform != "win32":
        print("  Nothing to do on this platform.")
        return 0
    if not _is_admin():
        print("  ERROR: administrator rights are required. Re-launch via hokku_setup.bat.")
        return 1

    state = load_state()
    name = state.get("task_name", TASK_NAME)
    rules = state.get("firewall_rules") or list(
        firewall_rule_names(state.get("port", DEFAULT_PORT))
    )

    unregister_scheduled_task(name)
    remove_firewall_rules(rules)
    try:
        STATE_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    install_dir = state.get("install_dir")
    print("  Removed the Scheduled Task and firewall rules.")
    if install_dir:
        print(f"  Left your photos and config in {install_dir} (delete it by hand if you want).")
    return 0
