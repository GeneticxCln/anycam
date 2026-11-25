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

import ipaddress
import json
import os
import re
import shutil
from pathlib import Path

UFW_DIR = Path("/etc/ufw")
UFW_DEFAULTS = Path("/etc/default/ufw")
COMMENT = "AnyCam phone camera"


def _validate_port(port: int) -> int:
    if isinstance(port, bool):
        raise ValueError(f"invalid port: {port!r}")
    value = int(port)
    if not (1 <= value <= 65535):
        raise ValueError(f"port out of range: {value}")
    return value


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def ufw_rules_readable(*, ufw_dir: Path = UFW_DIR, rules_file: str = "user.rules") -> bool:
    """Whether this user may read ufw's rules.

    Debian and Ubuntu install ``user.rules`` as mode 640, owner root: a normal
    user cannot see which ports are open.  (``ufw.conf`` and ``/etc/default/ufw``
    are world-readable, so "enabled, default deny" is still known.)
    """
    try:
        (ufw_dir / rules_file).read_text(encoding="utf-8")
    except PermissionError:
        return False
    except OSError:
        return True         # missing file: nothing hidden, just no rules
    return True


def _allowed_path() -> Path:
    from .. import config

    return config.state_dir() / "firewall-allowed.json"


def remember_allowed(port: int, *, state_file: Path | None = None,
                     backend_name: str = "ufw") -> None:
    """Note that this app opened ``port`` in ``backend_name`` (rules unreadable afterwards)."""
    port_num = _validate_port(port)
    path = state_file or _allowed_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        data = raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        data = {}
    ports = [int(p) for p in data.get(backend_name, []) if isinstance(p, (int, str)) and str(p).isdigit()]
    if port_num not in ports:
        ports.append(port_num)
        data[backend_name] = ports
        tmp = path.with_suffix(".tmp")
        fd = -1
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                json.dump(data, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _allowed_ports(path: Path, backend_name: str = "ufw") -> list[int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [int(p) for p in data.get(backend_name, [])]
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def _port_matches(spec: str, port: int) -> bool:
    if spec == "any":
        return True
    for part in spec.split(","):
        low, _, high = part.partition(":")
        try:
            if int(low) <= port <= int(high or low):
                return True
        except ValueError:
            continue
    return False


def _dest_allows(daddr: str, host: str = "") -> bool:
    if daddr in ("0.0.0.0/0", "::/0", "any"):
        return True
    if not host:
        return False
    try:
        net = ipaddress.ip_network(daddr, strict=False)
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return net.version == addr.version and addr in net


def _source_allows(saddr: str, host: str = "") -> bool:
    if saddr in ("0.0.0.0/0", "::/0", "any"):
        return True
    if not host:
        return False
    try:
        net = ipaddress.ip_network(saddr, strict=False)
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return net.version == addr.version and net.prefixlen < net.max_prefixlen and addr in net


def _rules_allow(rules: str, port: int, host: str = "") -> bool:
    # ufw records each rule as:
    # "### tuple ### allow <proto> <dport> <daddr> <sport> <saddr> <direction> ..."
    # ("any" instead of "tcp" for a protocol-less rule; ranges as a:b).
    pattern = re.compile(
        r"^### tuple ### allow (tcp|any) (\S+) (\S+) (\S+) (\S+) (\S+)",
        re.M,
    )
    for match in pattern.finditer(rules):
        _proto, dport, daddr, sport, saddr, direction = match.groups()
        if direction != "in" and not direction.startswith("in_"):
            continue
        if sport != "any":
            continue
        if not _port_matches(dport, port):
            continue
        if not _dest_allows(daddr, host):
            continue
        if not _source_allows(saddr, host):
            continue
        return True
    return False


def _is_ipv6(host: str) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).version == 6
    except ValueError:
        return False


def ufw_blocks(port: int, *, ufw_dir: Path = UFW_DIR,
               defaults: Path = UFW_DEFAULTS,
               state_file: Path | None = None,
               host: str = "") -> bool:
    """True when ufw is on, drops incoming traffic and may have no rule for ``port``.

    When the rules cannot be read (the normal case on Debian/Ubuntu) there is no
    way to prove the port is open, so the answer is "blocked" unless AnyCam
    opened it itself earlier; opening it again is harmless (``ufw allow`` is
    idempotent).  Unknown for other reasons (no ufw, disabled, default allow)
    is reported as not blocked: the dialog then shows no button rather than a
    wrong one.
    """
    port_num = _validate_port(port)
    if shutil.which("ufw") is None and not (ufw_dir / "ufw.conf").exists():
        return False
    if not re.search(r"^ENABLED=yes", _read(ufw_dir / "ufw.conf"), re.M):
        return False
    defaults_text = _read(defaults)
    policy = re.search(r'^DEFAULT_INPUT_POLICY="?(\w+)', defaults_text, re.M)
    if policy is None or policy.group(1).upper() == "ACCEPT":
        return False
    rules_file = "user6.rules" if _is_ipv6(host) else "user.rules"
    if rules_file == "user.rules":
        readable = ufw_rules_readable(ufw_dir=ufw_dir)
    else:
        readable = ufw_rules_readable(ufw_dir=ufw_dir, rules_file=rules_file)
    if not readable:
        return port_num not in _allowed_ports(state_file or _allowed_path())
    rules = _read(ufw_dir / rules_file)
    if not rules:
        if rules_file == "user.rules" and (ufw_dir / "user6.rules").exists():
            rules = _read(ufw_dir / "user6.rules")
        if not rules:
            return False
    return not _rules_allow(rules, port_num, host)


def allow_command(port: int) -> list[str] | None:
    """The command that opens ``port`` (asks for the password), or None."""
    port_num = _validate_port(port)
    pkexec, ufw = shutil.which("pkexec"), shutil.which("ufw")
    if pkexec is None or ufw is None:
        return None
    return [pkexec, ufw, "allow", f"{port_num}/tcp", "comment", COMMENT]


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
        try:
            zone = _call(FIREWALLD_PATH, FIREWALLD + ".zone", "getZoneOfInterface",
                         "(s)", (interface,), "(s)")[0]
        except Exception as exc:  # noqa: BLE001 - interface may not be bound to any zone
            if "UNKNOWN_INTERFACE" not in str(exc):
                raise
    if not zone:
        zone = _call(FIREWALLD_PATH, FIREWALLD, "getDefaultZone", reply="(s)")[0]
    return zone


def firewalld_blocks(port: int, host: str = "") -> bool:
    """True when firewalld's zone for the phone link does not open ``port``."""
    try:
        port_num = _validate_port(port)
        zone = firewalld_zone(host)
        opened = _call(FIREWALLD_PATH, FIREWALLD + ".zone", "queryPort",
                       "(sss)", (zone, str(port_num), "tcp"), "(b)")[0]
    except Exception:  # noqa: BLE001 - unknown is not "blocked" (no wrong button)
        return False
    return not opened


def firewalld_allow(port: int, host: str = "") -> str:
    """Open ``port``/tcp now and permanently; "" on success, else the reason."""
    try:
        port_num = _validate_port(port)
        zone = firewalld_zone(host)
        for step in ("runtime", "permanent"):
            try:
                if step == "runtime":
                    _call(FIREWALLD_PATH, FIREWALLD + ".zone", "addPort", "(sssi)",
                          (zone, str(port_num), "tcp", 0), "(s)", interactive=True)
                else:
                    config_zone = _call(FIREWALLD_CONFIG, FIREWALLD + ".config",
                                        "getZoneByName", "(s)", (zone,), "(o)")[0]
                    _call(config_zone, FIREWALLD + ".config.zone", "addPort",
                          "(ss)", (str(port_num), "tcp"), interactive=True)
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


# --------------------------------------------------------------- nftables
NFTABLES_CONF = Path("/etc/nftables.conf")


def _nftables_active(*, conf: Path = NFTABLES_CONF) -> bool:
    """True when plain nftables appears configured with a restrictive input policy."""
    if shutil.which("nft") is None:
        return False
    text = _read(conf)
    if not text:
        return False
    return bool(re.search(r"hook\s+input\b[^}]*policy\s+(drop|reject)", text, re.S | re.I))


def nftables_blocks(port: int, *, conf: Path = NFTABLES_CONF,
                    state_file: Path | None = None) -> bool:
    """True when ``/etc/nftables.conf`` drops input and does not accept ``port``/tcp."""
    port_num = _validate_port(port)
    if not _nftables_active(conf=conf):
        return False
    if port_num in _allowed_ports(state_file or _allowed_path(), "nftables"):
        return False
    text = _read(conf)
    if re.search(rf"\btcp\s+dport\s+(?:\{{[^}}]*\b)?{port_num}\b[^;\n]*\baccept\b", text):
        return False
    return True


def nftables_allow_command(port: int) -> list[str] | None:
    """Command (via pkexec) to accept ``port``/tcp in active nftables input filter chains."""
    port_num = _validate_port(port)
    pkexec, nft = shutil.which("pkexec"), shutil.which("nft")
    if pkexec is None or nft is None:
        return None
    script = (
        f"nft list chains | awk '"
        f"/^table / {{ fam=$2; tbl=$3 }} "
        f"/^\\tchain / {{ ch=$2 }} "
        f"/type filter hook input/ {{ print fam, tbl, ch }}' | "
        f"while read -r fam tbl ch; do "
        f"nft list chain \"$fam\" \"$tbl\" \"$ch\" | grep -q \"tcp dport {port_num} .*accept\" || "
        f"nft insert rule \"$fam\" \"$tbl\" \"$ch\" tcp dport {port_num} accept comment \\\"{COMMENT}\\\"; "
        f"done"
    )
    return [pkexec, "/bin/sh", "-c", script]


# --------------------------------------------------------------- dispatch
def backend() -> str:
    """"firewalld", "ufw", "nftables" or "" (nothing AnyCam knows how to open)."""
    if firewalld_running():
        return "firewalld"
    if shutil.which("ufw") is not None or (UFW_DIR / "ufw.conf").exists():
        return "ufw"
    if _nftables_active():
        return "nftables"
    return ""


def blocks(port: int, host: str = "") -> bool:
    """Whether the active firewall blocks the phone port (unknown = no)."""
    kind = backend()
    if kind == "firewalld":
        return firewalld_blocks(port, host)
    if kind == "ufw":
        return ufw_blocks(port, host=host) and allow_command(port) is not None
    if kind == "nftables":
        return nftables_blocks(port) and nftables_allow_command(port) is not None
    return False


# The polkit-agent helpers live in anycam.privileged (shared with the
# virtual camera setup); re-exported for the phone dialog.
from ..privileged import installed_agent, no_agent  # noqa: E402,F401
