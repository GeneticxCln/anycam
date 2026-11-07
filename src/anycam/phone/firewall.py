"""Let phones through the desktop firewall without a terminal.

Two backends, picked by :func:`backend`:

* **firewalld** (Fedora, openSUSE, ...): queried and changed over its D-Bus
  API (``org.fedoraproject.FirewallD1``); polkit asks for the password, no
  subprocess is involved.
* **ufw** (CachyOS, Ubuntu): it has no D-Bus API.  Its config is
  world-readable, so whether the port is blocked is checked without root, and
  opening it goes through ``pkexec ufw`` and the normal password prompt.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

UFW_DIR = Path("/etc/ufw")
UFW_DEFAULTS = Path("/etc/default/ufw")
COMMENT = "AnyCam phone camera"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def ufw_blocks(port: int, *, ufw_dir: Path = UFW_DIR,
               defaults: Path = UFW_DEFAULTS) -> bool:
    """True when ufw is on, drops incoming traffic and has no rule for ``port``.

    Unknown (unreadable config, no ufw) is reported as not blocked: the
    dialog then shows no button rather than a wrong one.
    """
    if shutil.which("ufw") is None and not (ufw_dir / "ufw.conf").exists():
        return False
    if not re.search(r"^ENABLED=yes", _read(ufw_dir / "ufw.conf"), re.M):
        return False
    policy = re.search(r'^DEFAULT_INPUT_POLICY="?(\w+)', _read(defaults), re.M)
    if policy is None or policy.group(1).upper() == "ACCEPT":
        return False
    rules = _read(ufw_dir / "user.rules")
    if not rules:
        return False
    # ufw records each rule as "### tuple ### allow tcp 8443 0.0.0.0/0 any ..."
    # ("any" instead of "tcp" for a protocol-less rule; ranges as a:b).
    for match in re.finditer(r"^### tuple ### allow (tcp|any) (\S+) ", rules, re.M):
        for part in match.group(2).split(","):
            low, _, high = part.partition(":")
            try:
                if int(low) <= port <= int(high or low):
                    return False
            except ValueError:
                continue
    return True


def allow_command(port: int) -> list[str] | None:
    """The command that opens ``port`` (asks for the password), or None."""
    pkexec, ufw = shutil.which("pkexec"), shutil.which("ufw")
    if pkexec is None or ufw is None:
        return None
    return [pkexec, ufw, "allow", f"{port}/tcp", "comment", COMMENT]


# ------------------------------------------------------------- firewalld
FIREWALLD = "org.fedoraproject.FirewallD1"
FIREWALLD_PATH = "/org/fedoraproject/FirewallD1"
FIREWALLD_CONFIG = "/org/fedoraproject/FirewallD1/config"
AUTH_TIMEOUT_MS = 120_000          # the user is typing a password


def _bus():
    from gi.repository import Gio

    return Gio.bus_get_sync(Gio.BusType.SYSTEM, None)


def _call(path: str, interface: str, method: str, signature: str = "",
          args: tuple = (), reply: str = "", *, interactive: bool = False):
    """One synchronous firewalld D-Bus call; returns the unpacked reply."""
    from gi.repository import Gio, GLib

    flags = (Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION if interactive
             else Gio.DBusCallFlags.NONE)
    result = _bus().call_sync(
        FIREWALLD, path, interface, method,
        GLib.Variant(signature, args) if signature else None,
        GLib.VariantType(reply) if reply else None,
        flags, AUTH_TIMEOUT_MS if interactive else 5000, None)
    return result.unpack() if result is not None else ()


def firewalld_running() -> bool:
    try:
        from gi.repository import Gio, GLib

        reply = _bus().call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "NameHasOwner", GLib.Variant("(s)", (FIREWALLD,)), GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE, 2000, None)
        return bool(reply.unpack()[0])
    except Exception:  # noqa: BLE001 - no system bus, no firewalld
        return False


def _interface_of(host: str) -> str:
    """The network interface that carries ``host`` (the phone link address)."""
    try:
        import psutil

        for name, addresses in psutil.net_if_addrs().items():
            if any(address.address == host for address in addresses):
                return name
    except Exception:  # noqa: BLE001
        pass
    return ""


def firewalld_zone(host: str = "") -> str:
    """The zone that applies to the phone link's interface (or the default)."""
    interface = _interface_of(host) if host else ""
    zone = ""
    if interface:
        zone = _call(FIREWALLD_PATH, FIREWALLD + ".zone", "getZoneOfInterface",
                     "(s)", (interface,), "(s)")[0]
    if not zone:
        zone = _call(FIREWALLD_PATH, FIREWALLD, "getDefaultZone", reply="(s)")[0]
    return zone


def firewalld_blocks(port: int, host: str = "") -> bool:
    """True when firewalld's zone for the phone link does not open ``port``."""
    try:
        zone = firewalld_zone(host)
        opened = _call(FIREWALLD_PATH, FIREWALLD + ".zone", "queryPort",
                       "(sss)", (zone, str(port), "tcp"), "(b)")[0]
    except Exception:  # noqa: BLE001 - unknown is not "blocked" (no wrong button)
        return False
    return not opened


def firewalld_allow(port: int, host: str = "") -> str:
    """Open ``port``/tcp now and permanently; "" on success, else the reason."""
    try:
        zone = firewalld_zone(host)
        for step in ("runtime", "permanent"):
            try:
                if step == "runtime":
                    _call(FIREWALLD_PATH, FIREWALLD + ".zone", "addPort", "(sssi)",
                          (zone, str(port), "tcp", 0), "(s)", interactive=True)
                else:
                    config_zone = _call(FIREWALLD_CONFIG, FIREWALLD + ".config",
                                        "getZoneByName", "(s)", (zone,), "(o)")[0]
                    _call(config_zone, FIREWALLD + ".config.zone", "addPort",
                          "(ss)", (str(port), "tcp"), interactive=True)
            except Exception as exc:  # noqa: BLE001
                if "ALREADY_ENABLED" not in str(exc):
                    raise
    except Exception as exc:  # noqa: BLE001 - reported to the user
        return str(exc)
    return ""


def auth_missing(reason: str) -> bool:
    """firewalld refused because polkit had nobody to ask for a password."""
    text = (reason or "").lower()
    return ("interactiveauthorizationrequired" in text or "notauthorized" in text
            or "not authorized" in text)


# --------------------------------------------------------------- dispatch
def backend() -> str:
    """"firewalld", "ufw" or "" (nothing AnyCam knows how to open)."""
    if firewalld_running():
        return "firewalld"
    if shutil.which("ufw") is not None or (UFW_DIR / "ufw.conf").exists():
        return "ufw"
    return ""


def blocks(port: int, host: str = "") -> bool:
    """Whether the active firewall blocks the phone port (unknown = no)."""
    kind = backend()
    if kind == "firewalld":
        return firewalld_blocks(port, host)
    if kind == "ufw":
        return ufw_blocks(port) and allow_command(port) is not None
    return False


# The polkit-agent helpers live in anycam.privileged (shared with the
# virtual camera setup); re-exported for the phone dialog.
from ..privileged import installed_agent, no_agent  # noqa: E402,F401
