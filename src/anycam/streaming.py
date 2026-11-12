"""Streaming destinations: platform presets and URL checks.

Pure helpers shared by the window and the engine so a typo is caught before a
broadcast, not after it fails on the server.
"""
from __future__ import annotations

from urllib.parse import urlsplit

# (label, ingest URL base).  The stream key is appended after a slash.
PRESETS: dict[str, tuple[str, str]] = {
    "youtube": ("YouTube", "rtmp://a.rtmp.youtube.com/live2"),
    "twitch": ("Twitch", "rtmp://live.twitch.tv/app"),
    "facebook": ("Facebook", "rtmps://live-api-s.facebook.com:443/rtmp"),
    "kick": ("Kick", "rtmps://fa723fc1b171.global-contribute.live-video.net:443/app"),
    "custom": ("Custom", ""),
}
SCHEMES = ("rtmp", "rtmps", "srt")


def is_srt(url: str) -> bool:
    return (url or "").strip().lower().startswith("srt://")


def preset_url(preset: str, key: str) -> str:
    """The full publish URL for ``preset`` and a stream ``key`` ("" when custom)."""
    base = PRESETS.get(preset, PRESETS["custom"])[1]
    key = (key or "").strip().strip("/")
    if not base:
        return ""
    return f"{base}/{key}" if key else base


def split_url(url: str) -> tuple[str, str]:
    """(preset id, key) when ``url`` starts with a known ingest base, else ("custom", url)."""
    text = (url or "").strip()
    for preset, (_label, base) in PRESETS.items():
        if base and text.startswith(base):
            return preset, text[len(base):].strip("/")
    return "custom", text


def validate_stream_url(url: str) -> str:
    """"" when ``url`` can be published to, otherwise what is wrong with it."""
    text = (url or "").strip()
    if not text:
        return "the stream address is empty"
    forbidden = " \t\r\n\"'`;|<>\\" + ("" if is_srt(text) else "&")
    if any(ch in text for ch in forbidden):
        return "the stream address contains spaces or shell characters"
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return "the stream address is not a valid URL"
    if parts.scheme.lower() not in SCHEMES:
        return "the stream address must start with rtmp://, rtmps:// or srt://"
    if not parts.hostname:
        return "the stream address has no host"
    if port is not None and not 0 < port < 65536:
        return "the stream address has an invalid port"
    if parts.scheme.lower() == "srt":
        return "" if port else "an srt:// address needs a port, like srt://host:9000"
    if parts.path.strip("/") == "":
        return "the stream address has no application or stream key"
    return ""
