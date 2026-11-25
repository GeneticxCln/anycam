"""Content sources: PDF slides and video-site links.

Both lean on a command-line tool when it is installed (``pdftoppm`` from poppler,
``yt-dlp``) and say plainly when it is not, instead of adding a heavy dependency.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
import shutil
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from . import config

MAX_PDF_PAGES = 200
PDF_CACHE_MAX_FOLDERS = 20
_VIDEO_SITES = re.compile(
    r"(^|\.)(youtube\.com|youtu\.be|twitch\.tv|vimeo\.com|dailymotion\.com|"
    r"facebook\.com|twitter\.com|x\.com|tiktok\.com|kick\.com)$", re.I)
_BLOCKED_HOSTS = frozenset({
    "localhost", "metadata", "metadata.goog", "metadata.google.internal",
    "instance-data",
})


def pdf_available() -> bool:
    return shutil.which("pdftoppm") is not None


def slides_available() -> bool:
    """True when LibreOffice is installed for PPTX/ODP slide conversion."""
    return (shutil.which("soffice") is not None
            or shutil.which("libreoffice") is not None)


def presentation_pages(
    path: str, dpi: int = 110, max_pages: int = MAX_PDF_PAGES
) -> tuple[list[Path], str]:
    """Convert a PPTX/ODP presentation via LibreOffice headless into slide PNGs."""
    import tempfile

    source = Path(path).expanduser()
    if not source.is_file():
        return [], f"Presentation not found: {source}"
    if source.suffix.lower() == ".pdf":
        return pdf_pages(str(source), dpi=dpi, max_pages=max_pages)
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice is None:
        return [], "reading PPTX/ODP slides needs LibreOffice (the soffice command)"
    if not pdf_available():
        return [], "reading slides needs poppler-utils (the pdftoppm command)"
    try:
        stat = source.stat()
    except OSError as exc:
        return [], f"Presentation not readable: {exc}"
    digest = hashlib.sha256(
        f"pres|{source.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{dpi}|{max_pages}".encode()
    ).hexdigest()[:16]
    pdf_root = config.cache_dir() / "pdf"
    cached_pdf = pdf_root / f"pres-{digest}.pdf"
    if not cached_pdf.is_file():
        pdf_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="anycam-lo-") as tmpdir:
            profile = Path(tmpdir) / "profile"
            outdir = Path(tmpdir) / "out"
            outdir.mkdir()
            try:
                result = subprocess.run(
                    [
                        soffice,
                        "--headless",
                        "--norestore",
                        f"-env:UserInstallation={profile.as_uri()}",
                        "--convert-to", "pdf",
                        "--outdir", str(outdir),
                        "--", str(source),
                    ],
                    capture_output=True, text=True, timeout=180,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return [], f"LibreOffice failed: {exc}"
            produced = sorted(outdir.glob("*.pdf"))
            if result.returncode != 0 or not produced:
                detail = result.stderr.strip()[-160:] or result.stdout.strip()[-160:] or "unknown error"
                return [], f"LibreOffice could not convert the presentation: {detail}"
            tmp_pdf = pdf_root / f".pres-{digest}.tmp.pdf"
            shutil.copy2(produced[0], tmp_pdf)
            tmp_pdf.replace(cached_pdf)
    return pdf_pages(str(cached_pdf), dpi=dpi, max_pages=max_pages)


def _prune_pdf_cache(root: Path, keep: Path, max_folders: int = PDF_CACHE_MAX_FOLDERS) -> None:
    try:
        folders = [(p, p.stat().st_mtime) for p in root.iterdir() if p.is_dir()]
    except OSError:
        return
    if len(folders) <= max_folders:
        return
    folders.sort(key=lambda item: item[1])
    to_remove = len(folders) - max_folders
    for folder, _mtime in folders:
        if to_remove <= 0:
            break
        if folder == keep:
            continue
        shutil.rmtree(folder, ignore_errors=True)
        to_remove -= 1


def _page_sort_key(path: Path) -> tuple[int, str]:
    suffix = path.stem.rsplit("-", 1)[-1]
    return (int(suffix) if suffix.isdigit() else 10**9, path.name)


def pdf_pages(path: str, dpi: int = 110, max_pages: int = MAX_PDF_PAGES) -> tuple[list[Path], str]:
    """(page pictures, error): each page as a PNG in the cache, in page order.

    Pages already rendered for the same file are reused.
    """
    import os
    import tempfile

    source = Path(path).expanduser()
    if not source.is_file():
        return [], f"PDF not found: {source}"
    if not pdf_available():
        return [], "reading a PDF needs poppler-utils (the pdftoppm command)"
    dpi = max(36, min(300, int(dpi)))
    max_pages = max(1, min(500, int(max_pages)))
    try:
        stat = source.stat()
    except OSError as exc:
        return [], f"PDF not readable: {exc}"
    digest = hashlib.sha256(
        f"{source.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{dpi}|{max_pages}".encode()
    ).hexdigest()[:16]
    pdf_root = config.cache_dir() / "pdf"
    folder = pdf_root / digest
    # Only a render that completed is reused: pages are rendered into a
    # private staging folder and moved into place atomically so concurrent
    # callers and timed-out renders never leave partial pages cached.
    pages = sorted(folder.glob("page-*.png"), key=_page_sort_key) if (folder / ".complete").exists() else []
    if pages:
        try:
            os.utime(folder, None)  # LRU refresh
        except OSError:
            pass
    if not pages:
        pdf_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{digest}-", dir=str(pdf_root)))
        try:
            try:
                result = subprocess.run(
                    ["pdftoppm", "-png", "-r", str(dpi), "-l", str(max_pages),
                     "--", str(source), str(staging / "page")],
                    capture_output=True, text=True, timeout=180)
            except (OSError, subprocess.TimeoutExpired) as exc:
                return [], f"pdftoppm failed: {exc}"
            if result.returncode != 0:
                return [], "pdftoppm could not read the file: " + (result.stderr.strip()[-160:] or "unknown error")
            staged = sorted(staging.glob("page-*.png"), key=_page_sort_key)
            if not staged:
                return [], "the PDF has no pages"
            (staging / ".complete").write_text("done\n")
            if folder.exists():
                shutil.rmtree(folder, ignore_errors=True)
            try:
                staging.replace(folder)
            except OSError:
                # Another caller raced and populated ``folder`` first.
                pass
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        pages = sorted(folder.glob("page-*.png"), key=_page_sort_key) if (folder / ".complete").exists() else []
        if pages:
            _prune_pdf_cache(pdf_root, keep=folder)
    return (pages, "") if pages else ([], "the PDF has no pages")


def ytdlp_available() -> bool:
    return shutil.which("yt-dlp") is not None


def _has_bad_url_chars(url: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() for ch in url)


def _is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        if _is_forbidden_ip(ip.ipv4_mapped):
            return True
    return bool(
        ip.is_loopback or ip.is_link_local or ip.is_private
        or ip.is_unspecified or ip.is_reserved or ip.is_multicast
    )


def _is_safe_stream_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    host = hostname.strip().rstrip(".").lower()
    if not host or host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            packed = socket.inet_aton(host)
        except OSError:
            # Resolve DNS names so hostnames pointing at loopback/LAN/metadata
            # IPs (e.g. *.nip.io) cannot bypass the literal-IP guard.  An
            # unresolvable name (offline/mock host) is left to the caller.
            try:
                infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
            except OSError:
                return True
            for info in infos:
                addr = info[4][0]
                try:
                    resolved = ipaddress.ip_address(addr.split("%", 1)[0])
                except ValueError:
                    continue
                if _is_forbidden_ip(resolved):
                    return False
            return True
        ip = ipaddress.IPv4Address(packed)
    return not _is_forbidden_ip(ip)


def needs_resolving(url: str) -> bool:
    """Whether ``url`` is a video-site page rather than a stream address."""
    parts = urlsplit((url or "").strip())
    return parts.scheme in ("http", "https") and bool(_VIDEO_SITES.search(parts.hostname or ""))


def resolve_stream(url: str, timeout: float = 45.0) -> tuple[str, str]:
    """(direct stream address, error) for a video-site link, through ``yt-dlp -g``."""
    cleaned = str(url or "").strip()
    if not cleaned or _has_bad_url_chars(cleaned):
        return "", "invalid video URL"
    parts = urlsplit(cleaned)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "", "video link must be an http:// or https:// address"
    if not _is_safe_stream_host(parts.hostname):
        return "", "video link points to a disallowed local or private address"
    if not ytdlp_available():
        return "", "playing a video-site link needs yt-dlp (pip install yt-dlp)"
    try:
        result = subprocess.run(
            ["yt-dlp", "--ignore-config", "--no-playlist",
             "-f", "best[protocol^=http]/best", "-g", "--", cleaned],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", f"yt-dlp failed: {exc}"
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or not lines:
        return "", "yt-dlp could not open the link: " + (result.stderr.strip()[-160:] or "no stream")
    stream = lines[0]
    if _has_bad_url_chars(stream):
        return "", "yt-dlp returned an invalid stream address"
    stream_parts = urlsplit(stream)
    if stream_parts.scheme not in ("http", "https") or not stream_parts.hostname:
        return "", "yt-dlp returned something that is not a stream address"
    if not _is_safe_stream_host(stream_parts.hostname):
        return "", "yt-dlp returned a disallowed local or private stream address"
    return stream, ""
