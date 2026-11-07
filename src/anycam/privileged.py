"""Run a command as root through the desktop's password prompt (pkexec).

Some compositor sessions never start a polkit agent, so pkexec has no window
to ask for the password and fails before any prompt.  When that happens and
an agent is installed, it is started and the command is asked for once more.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

# Password-prompt agents a desktop may have installed but not running.
POLKIT_AGENTS = (
    "/usr/lib/polkit-kde-authentication-agent-1",
    "/usr/libexec/polkit-kde-authentication-agent-1",
    "/usr/lib/x86_64-linux-gnu/libexec/polkit-kde-authentication-agent-1",
    "/usr/lib/polkit-gnome/polkit-gnome-authentication-agent-1",
    "/usr/libexec/polkit-gnome-authentication-agent-1",
    "/usr/lib/mate-polkit/polkit-mate-authentication-agent-1",
    "/usr/bin/lxpolkit",
)
AGENT_START_S = 2.5
# pkexec: 126 = the prompt was dismissed, 127 = not authorized / no agent.
CANCELLED = (126, 127)


def no_agent(stderr: str) -> bool:
    """pkexec found no graphical password prompt.

    Without a polkit agent pkexec tries its own terminal prompt; started from
    a GUI that fails with "Error creating textual authentication agent"
    (with ``--disable-internal-agent``: "No authentication agent found").
    """
    text = (stderr or "").lower()
    return ("no authentication agent" in text
            or "textual authentication agent" in text)


def installed_agent() -> str | None:
    for path in POLKIT_AGENTS:
        if Path(path).is_file():
            return path
    return None


def start_agent(agent: str) -> None:
    # No pipes: the agent outlives this call and must not hold our output.
    subprocess.Popen([agent], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def run_as_root(args: list[str], *, timeout: float = 180) -> subprocess.CompletedProcess:
    """Run ``args`` as root via pkexec, starting a polkit agent if needed."""
    if shutil.which("pkexec") is None:
        raise RuntimeError("pkexec is not installed")
    command = ["pkexec", *args]
    result = subprocess.run(command, capture_output=True, text=True,
                            timeout=timeout, check=False)
    agent = installed_agent()
    if result.returncode != 0 and no_agent(result.stderr) and agent:
        start_agent(agent)
        time.sleep(AGENT_START_S)
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=timeout, check=False)
    return result


def failure_reason(result: subprocess.CompletedProcess) -> str:
    output = (result.stderr or result.stdout or "").strip()
    if no_agent(output):
        return "this desktop has no password prompt (polkit agent) running"
    if result.returncode == 126:
        return "the password prompt was cancelled"
    lines = output.splitlines()
    return lines[-1] if lines else f"exit code {result.returncode}"
