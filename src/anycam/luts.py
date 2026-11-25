"""Adobe/Iridas ``.cube`` 3D LUT parser and vectorized trilinear applicator."""
from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import config

MAX_LUT_SIZE = 65


@dataclass(frozen=True)
class CubeLut:
    """Parsed 3D colour lookup table in R-fastest (cube standard) order."""

    title: str
    size: int
    domain_min: tuple[float, float, float]
    domain_max: tuple[float, float, float]
    # Indexed as table[b, g, r, c] in 0..1 float32 for fast trilinear lookup.
    table: np.ndarray


def parse_cube(text: str, default_title: str = "LUT") -> CubeLut:
    """Parse ``.cube`` 3D LUT text into a :class:`CubeLut`."""
    title = default_title
    size = 0
    domain_min = (0.0, 0.0, 0.0)
    domain_max = (1.0, 1.0, 1.0)
    rows: list[tuple[float, float, float]] = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        upper = line.upper()
        if upper.startswith("TITLE"):
            quoted = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            title = quoted.strip('"').strip("'") or default_title
            continue
        if upper.startswith("LUT_3D_SIZE"):
            parts = line.split()
            if len(parts) < 2:
                raise ValueError("LUT_3D_SIZE missing value")
            size = int(parts[1])
            if not (2 <= size <= MAX_LUT_SIZE):
                raise ValueError(f"LUT_3D_SIZE out of range (2..{MAX_LUT_SIZE}): {size}")
            continue
        if upper.startswith("LUT_1D_SIZE"):
            raise ValueError("1D .cube LUTs are not supported; provide a 3D .cube LUT")
        if upper.startswith("DOMAIN_MIN"):
            parts = line.split()[1:4]
            if len(parts) == 3:
                domain_min = (float(parts[0]), float(parts[1]), float(parts[2]))
            continue
        if upper.startswith("DOMAIN_MAX"):
            parts = line.split()[1:4]
            if len(parts) == 3:
                domain_max = (float(parts[0]), float(parts[1]), float(parts[2]))
            continue
        parts = line.split()
        if len(parts) == 3:
            rows.append((float(parts[0]), float(parts[1]), float(parts[2])))

    if size <= 0:
        raise ValueError("missing LUT_3D_SIZE header")
    expected = size ** 3
    if len(rows) != expected:
        raise ValueError(f"expected {expected} RGB entries for size {size}, got {len(rows)}")
    # Standard .cube order iterates R fastest, then G, then B -> shape (B, G, R, 3).
    table = np.asarray(rows, dtype=np.float32).reshape(size, size, size, 3)
    table = np.clip(table, 0.0, 1.0)
    return CubeLut(
        title=title,
        size=size,
        domain_min=domain_min,
        domain_max=domain_max,
        table=table,
    )


@functools.lru_cache(maxsize=16)
def _load_cached(resolved: str, mtime_ns: int, size_bytes: int) -> CubeLut:
    path = Path(resolved)
    return parse_cube(path.read_text(encoding="utf-8", errors="replace"),
                      default_title=path.stem)


def load_cube(path: str | Path) -> CubeLut:
    """Load and cache a ``.cube`` 3D LUT file from disk."""
    target = Path(path).expanduser().resolve()
    stat = target.stat()
    return _load_cached(str(target), stat.st_mtime_ns, stat.st_size)


def luts_dir() -> Path:
    """Directory where user ``.cube`` LUT files are stored."""
    return config.state_dir() / "luts"


def discover_luts(folder: Path | None = None) -> list[Path]:
    """Return sorted ``.cube`` files in ``luts_dir()``."""
    root = folder if folder is not None else luts_dir()
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_file() and p.suffix.lower() == ".cube")


def apply_cube_lut(rgb: np.ndarray, lut: CubeLut, strength: float = 1.0) -> np.ndarray:
    """Apply a 3D LUT to an ``(H, W, 3)`` uint8 RGB image via trilinear interpolation."""
    alpha = float(max(0.0, min(1.0, strength)))
    if alpha <= 1e-4:
        return rgb
    src = rgb.astype(np.float32) * (1.0 / 255.0)
    dmin = np.asarray(lut.domain_min, dtype=np.float32)
    dmax = np.asarray(lut.domain_max, dtype=np.float32)
    span = np.maximum(dmax - dmin, 1e-6)
    norm = np.clip((src - dmin) / span, 0.0, 1.0) * (lut.size - 1)

    r_f = norm[..., 0]
    g_f = norm[..., 1]
    b_f = norm[..., 2]
    r0 = np.floor(r_f).astype(np.int32)
    g0 = np.floor(g_f).astype(np.int32)
    b0 = np.floor(b_f).astype(np.int32)
    r1 = np.minimum(r0 + 1, lut.size - 1)
    g1 = np.minimum(g0 + 1, lut.size - 1)
    b1 = np.minimum(b0 + 1, lut.size - 1)
    dr = (r_f - r0)[..., None]
    dg = (g_f - g0)[..., None]
    db = (b_f - b0)[..., None]

    t = lut.table
    c000 = t[b0, g0, r0]
    c001 = t[b0, g0, r1]
    c010 = t[b0, g1, r0]
    c011 = t[b0, g1, r1]
    c100 = t[b1, g0, r0]
    c101 = t[b1, g0, r1]
    c110 = t[b1, g1, r0]
    c111 = t[b1, g1, r1]

    c00 = c000 + (c001 - c000) * dr
    c01 = c010 + (c011 - c010) * dr
    c10 = c100 + (c101 - c100) * dr
    c11 = c110 + (c111 - c110) * dr
    c0 = c00 + (c01 - c00) * dg
    c1 = c10 + (c11 - c10) * dg
    mapped = c0 + (c1 - c0) * db

    if alpha < 0.999:
        mapped = src * (1.0 - alpha) + mapped * alpha
    return np.clip(np.round(mapped * 255.0), 0, 255).astype(np.uint8)
