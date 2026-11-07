"""Let phones through the desktop firewall without a terminal.

Only ufw is handled (CachyOS/Ubuntu default).  Its config is world-readable,
so whether the phone port is blocked can be checked without root; opening it
goes through ``pkexec`` and the desktop's normal password prompt.
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


# The polkit-agent helpers live in anycam.privileged (shared with the
# virtual camera setup); re-exported for the phone dialog.
from ..privileged import installed_agent, no_agent  # noqa: E402,F401
