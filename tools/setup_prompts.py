"""Shared interactive-prompt and input-validation helpers.

Extracted from ``pi_installer`` so both the Raspberry Pi installer and the
local-PC installer (``local_installer``) can reuse them without one importing
the other. Everything here is platform-agnostic — no Windows or Pi specifics —
so it stays importable everywhere the wizard runs.

Validators return ``(ok: bool, reason: str)``. Prompt helpers loop until the
user supplies a value that validates.
"""

from __future__ import annotations

import getpass
import zoneinfo

# ---------- input validation ----------

# Shared safe character set: printable ASCII (0x20-0x7E) minus characters that
# are hard to embed safely across the contexts we interpolate into: bash
# double-quoted strings, bash heredocs, NetworkManager .nmconnection ini values,
# and wpa_supplicant double-quoted string values.
_DISALLOWED_ANY = set('"\\\n\r')

# Sticky install settings persisted in .cache/settings.json and shared between
# the Pi and local-PC installers, so WiFi credentials etc. pre-fill across runs
# regardless of which install path was used.
_STICKY_KEYS = (
    "wifi_ssid",
    "wifi_pass",
    "user",
    "password",
    "ssh_enabled",
    "samba",
    "country",
    "timezone",
    "mdns_hostname",
    "local_install_dir",
    "local_port",
)


def _bad_chars(s, extra_disallowed=""):
    """Return sorted list of disallowed/unprintable characters found in `s`."""
    bad = set()
    for ch in s:
        code = ord(ch)
        if code < 0x20 or code > 0x7E:
            bad.add(ch)
        elif ch in _DISALLOWED_ANY or ch in extra_disallowed:
            bad.add(ch)
    return sorted(bad)


def _char_report(chars):
    """Human-readable list of bad characters — shows repr so control chars visible."""
    return ", ".join(repr(c) for c in chars)


def validate_ssid(s):
    """Return (ok, reason). WPA SSID: 1-32 bytes, no `"`, `\\`, newlines, no non-printable."""
    if not s:
        return False, "SSID is empty"
    if len(s.encode("utf-8")) > 32:
        return False, f"SSID is {len(s.encode('utf-8'))} bytes (max 32)"
    bad = _bad_chars(s)
    if bad:
        return False, f"SSID contains disallowed characters: {_char_report(bad)}"
    return True, ""


def validate_wifi_password(s):
    """Return (ok, reason). WPA2 PSK: 8-63 printable ASCII (or empty for open network)."""
    if s == "":
        return True, ""  # open network
    if len(s) < 8:
        return False, "WiFi password must be at least 8 characters (WPA2 PSK requirement)"
    if len(s) > 63:
        return False, f"WiFi password is {len(s)} characters (max 63)"
    bad = _bad_chars(s)
    if bad:
        return False, f"WiFi password contains disallowed characters: {_char_report(bad)}"
    return True, ""


def validate_mdns_hostname(s):
    """Return (ok, reason). Valid mDNS label: a-z0-9 and hyphens, no leading/trailing hyphen."""
    if not s:
        return False, "Hostname is empty"
    if len(s) > 63:
        return False, f"Hostname is {len(s)} chars (max 63)"
    if not s[0].isalnum():
        return False, "Hostname must start with a letter or digit"
    if s[-1] == "-":
        return False, "Hostname must not end with a hyphen"
    for ch in s.lower():
        if not (ch.isalnum() or ch == "-"):
            return False, f"Hostname contains disallowed character: {ch!r} (allowed: a-z 0-9 -)"
    return True, ""


def validate_country_code(s):
    """Return (ok, reason). ISO 3166-1 alpha-2, used by `raspi-config nonint
    do_wifi_country`. Two uppercase letters A-Z."""
    if not s:
        return False, "Country code is empty"
    if len(s) != 2:
        return False, f"Country code must be 2 letters (got {len(s)})"
    if not (s.isascii() and s.isalpha() and s.isupper()):
        return False, f"Country code must be 2 UPPERCASE ASCII letters (got {s!r})"
    return True, ""


def _available_timezones():
    """Return the IANA zone set from zoneinfo, or None if it's not usable.

    Windows ships no system tzdata, so zoneinfo.available_timezones() returns
    an empty set unless the `tzdata` PyPI package is installed. Treat empty
    as 'unavailable' so we fall through to the format-only check rather than
    rejecting every valid zone.
    """
    try:
        tzs = set(zoneinfo.available_timezones())
        return tzs if tzs else None
    except Exception:
        return None


def validate_timezone(s):
    """Return (ok, reason). IANA zone name like Europe/London. If system
    tzdata is available, validate strictly against it; otherwise enforce the
    conventional Region/City format."""
    if not s:
        return False, "Timezone is empty"
    available = _available_timezones()
    if available is not None:
        if s in available:
            return True, ""
        return (
            False,
            f"{s!r} is not a known IANA timezone (e.g. Europe/London, America/New_York, UTC)",
        )
    # Fallback: enforce shape (word, or Region/City, or Region/Sub/City).
    # Valid characters per IANA zone names: A-Za-z0-9 _ - + (plus / separators).
    if " " in s or ".." in s:
        return False, f"Timezone looks malformed: {s!r} (expected e.g. Europe/London)"
    parts = s.split("/")
    for p in parts:
        if not p or not p[0].isalpha() or not all(c.isalnum() or c in "_-+" for c in p):
            return False, f"Timezone looks malformed: {s!r} (expected e.g. Europe/London)"
    return True, ""


# ---------- interactive prompts ----------


def _yesno(prompt, default_yes=True):
    suffix = "[Y/n]" if default_yes else "[y/N]"
    v = input(f"  {prompt} {suffix}: ").strip().lower()
    if not v:
        return default_yes
    return v in ("y", "yes")


def _prompt_validated(prompt, validator, hidden=False, default=None):
    """Prompt until the user supplies input that passes `validator(s) -> (ok, reason)`."""
    while True:
        if hidden:
            s = getpass.getpass(prompt)
        else:
            s = input(prompt)
        if not s and default is not None:
            s = default
        ok, reason = validator(s)
        if ok:
            return s
        print(f"  ERROR: {reason}")


def _masked(s):
    return "*" * len(s) if s else ""


def _prompt_with_sticky(prompt_label, validator, sticky, default, hidden=False):
    """Prompt with sticky-value fallback. If `sticky` is set, show it as the
    default (masked if `hidden`); empty input keeps it. Else use `default`."""
    if sticky:
        shown = _masked(sticky) if hidden else sticky
        suffix = f"[{shown}]"
    elif default is not None:
        suffix = f"[{default}]"
    else:
        suffix = ""
    p = f"  {prompt_label} {suffix}: " if suffix else f"  {prompt_label}: "
    while True:
        s = getpass.getpass(p) if hidden else input(p)
        if not s:
            s = sticky if sticky else (default if default is not None else "")
        ok, reason = validator(s)
        if ok:
            return s
        print(f"  ERROR: {reason}")
