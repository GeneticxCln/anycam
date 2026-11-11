"""Optional effect models: what can be downloaded, from where, and checks.

AnyCam ships the MediaPipe person segmenter (Apache-2.0).  Better models whose
licence does not allow bundling them in an MIT app are offered as a download
from their authors' own release, only when the user clicks Download, and are
verified against a pinned SHA-256 before use.  Nothing here runs on its own:
AnyCam makes no network request unless asked.
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import config


@dataclass(frozen=True)
class ModelInfo:
    key: str
    label: str
    filename: str
    url: str
    sha256: str
    size: int
    license: str
    source: str
    note: str = ""


CATALOG: dict[str, ModelInfo] = {
    "rvm": ModelInfo(
        key="rvm",
        label="Robust Video Matting (MobileNetV3)",
        filename="rvm_mobilenetv3_fp32.onnx",
        url=("https://github.com/PeterL1n/RobustVideoMatting/releases/download/"
             "v1.0.0/rvm_mobilenetv3_fp32.onnx"),
        sha256="88d4531297118f595bf2fd60f6f566aec2e559393802d1f436c380f0cbbd2828",
        size=14975696,
        license="GPL-3.0",
        source="https://github.com/PeterL1n/RobustVideoMatting",
        note=("Real matting with soft edges and hair, stable between frames. "
              "Downloaded from the author's release; not shipped with AnyCam "
              "(its GPL-3.0 licence does not allow bundling it in an MIT app)."),
    ),
}

ProgressHandler = Callable[[int, int], None]
_verified: dict[str, tuple[int, int]] = {}


def models_dir() -> Path:
    path = config.data_dir() / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def model_path(key: str) -> Path | None:
    """The downloaded file for ``key`` when it is present and complete."""
    info = CATALOG.get(key)
    if info is None:
        return None
    path = models_dir() / info.filename
    try:
        stat = path.stat()
        if stat.st_size != info.size:
            return None
        # Size alone accepts a replaced file; check the pinned hash once per
        # change of the file, not on every call.
        stamp = (stat.st_mtime_ns, stat.st_size)
        if _verified.get(str(path)) != stamp:
            if _sha256(path) != info.sha256:
                return None
            _verified[str(path)] = stamp
        return path
    except OSError:
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(key: str, progress: ProgressHandler | None = None,
             timeout: float = 30.0) -> Path:
    """Fetch model ``key``, verify it, and return its path.

    The file is written beside its final name and only renamed once its size
    and SHA-256 match, so an interrupted or tampered download never becomes
    the model AnyCam loads.
    """
    info = CATALOG[key]
    final = models_dir() / info.filename
    partial = final.with_suffix(final.suffix + ".part")
    request = urllib.request.Request(info.url, headers={"User-Agent": "AnyCam"})
    received = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, \
                partial.open("wb") as handle:
            while True:
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                handle.write(chunk)
                received += len(chunk)
                if received > info.size:
                    raise ValueError("the download is larger than the expected model")
                if progress is not None:
                    progress(received, info.size)
        if received != info.size:
            raise ValueError(f"the download stopped at {received} of {info.size} bytes")
        actual = _sha256(partial)
        if actual != info.sha256:
            raise ValueError("the downloaded file does not match the expected "
                             f"SHA-256 ({actual[:12]}…); it was discarded")
        os.replace(partial, final)
    finally:
        partial.unlink(missing_ok=True)
    return final


def remove(key: str) -> None:
    info = CATALOG.get(key)
    if info is not None:
        (models_dir() / info.filename).unlink(missing_ok=True)
