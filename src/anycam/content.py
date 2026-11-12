"""Content sources: PDF slides and video-site links.

Both lean on a command-line tool when it is installed (``pdftoppm`` from poppler,
``yt-dlp``) and say plainly when it is not, instead of adding a heavy dependency.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

from . import config

MAX_PDF_PAGES = 200
_VIDEO_SITES = re.compile(
    r"(^|\.)(youtube\.com|youtu\.be|twitch\.tv|vimeo\.com|dailymotion\.com|"
    r"facebook\.com|twitter\.com|x\.com|tiktok\.com|kick\.com)$", re.I)


def pdf_available() -> bool:
    return shutil.which("pdftoppm") is not None


def pdf_pages(path: str, dpi: int = 110, max_pages: int = MAX_PDF_PAGES) -> tuple[list[Path], str]:
    """(page pictures, error): each page as a PNG in the cache, in page order.

    Pages already rendered for the same file are reused.
    """
    source = Path(path).expanduser()
    if not source.is_file():
        return [], f"PDF not found: {source}"
    if not pdf_available():
        return [], "reading a PDF needs poppler-utils (the pdftoppm command)"
    stat = source.stat()
    digest = hashlib.sha1(f"{source.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{dpi}".encode()
                          ).hexdigest()[:16]
    folder = config.cache_dir() / "pdf" / digest
    pages = sorted(folder.glob("page-*.png"))
    if not pages:
        folder.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                ["pdftoppm", "-png", "-r", str(max(36, min(300, int(dpi)))), "-l", str(max_pages),
                 str(source), str(folder / "page")],
                capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return [], f"pdftoppm failed: {exc}"
        if result.returncode != 0:
            return [], "pdftoppm could not read the file: " + (result.stderr.strip()[-160:] or "unknown error")
        pages = sorted(folder.glob("page-*.png"))
    return (pages, "") if pages else ([], "the PDF has no pages")


def ytdlp_available() -> bool:
    return shutil.which("yt-dlp") is not None


def needs_resolving(url: str) -> bool:
    """Whether ``url`` is a video-site page rather than a stream address."""
    from urllib.parse import urlsplit

    parts = urlsplit((url or "").strip())
    return parts.scheme in ("http", "https") and bool(_VIDEO_SITES.search(parts.hostname or ""))


def resolve_stream(url: str, timeout: float = 45.0) -> tuple[str, str]:
    """(direct stream address, error) for a video-site link, through ``yt-dlp -g``."""
    if not ytdlp_available():
        return "", "playing a video-site link needs yt-dlp (pip install yt-dlp)"
    try:
        result = subprocess.run(
            ["yt-dlp", "--no-playlist", "-f", "best[protocol^=http]/best", "-g", url],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", f"yt-dlp failed: {exc}"
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or not lines:
        return "", "yt-dlp could not open the link: " + (result.stderr.strip()[-160:] or "no stream")
    stream = lines[0]
    if not stream.startswith(("http://", "https://")):
        return "", "yt-dlp returned something that is not a stream address"
    return stream, ""
