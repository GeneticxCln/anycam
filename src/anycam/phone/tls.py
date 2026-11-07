"""The phone link's self-signed certificate and the QR image."""

from __future__ import annotations

import hashlib
import io
import shutil
import socket
import ssl
import subprocess
from pathlib import Path


def ensure_certificate(directory: Path) -> tuple[Path, Path]:
    """Create (once) the self-signed certificate the phone page is served with.

    Phones only allow the camera on an HTTPS page.  There is no certificate
    authority for a LAN address, so this one is self-signed: the phone shows a
    warning once, and the desktop shows the fingerprint to compare.
    """
    directory = Path(directory)
    cert, key = directory / "phone-cert.pem", directory / "phone-key.pem"
    if cert.is_file() and key.is_file():
        return cert, key
    openssl = shutil.which("openssl")
    if openssl is None:
        raise RuntimeError("openssl is needed to create the phone link certificate")
    directory.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [openssl, "req", "-x509", "-newkey", "ec",
         "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
         "-days", "3650", "-subj", "/CN=AnyCam phone link",
         "-keyout", str(key), "-out", str(cert)],
        capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0 or not cert.is_file():
        raise RuntimeError("could not create the phone link certificate: "
                           + (result.stderr.strip() or "openssl failed"))
    key.chmod(0o600)
    return cert, key


def fingerprint(cert: Path) -> str:
    der = ssl.PEM_cert_to_DER_cert(Path(cert).read_text(encoding="ascii"))
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def lan_address() -> str:
    """This computer's address on the local network (the default route's)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            # No packet is sent; connect() on UDP only picks the route.
            probe.connect(("192.0.2.1", 9))
            address = probe.getsockname()[0]
        except OSError:
            address = ""
    if not address or address.startswith("127."):
        raise RuntimeError("no local network address: connect to Wi-Fi or Ethernet")
    return address


def qr_png(text: str, scale: int = 8) -> bytes | None:
    """A PNG QR code for ``text`` (segno if installed, else ``qrencode``)."""
    try:
        import segno  # type: ignore[import-not-found]
    except ImportError:
        segno = None
    if segno is not None:
        buffer = io.BytesIO()
        segno.make(text, error="m").save(buffer, kind="png", scale=scale, border=4)
        return buffer.getvalue()
    exe = shutil.which("qrencode")
    if exe is None:
        return None
    result = subprocess.run([exe, "-t", "PNG", "-l", "M", "-s", str(scale),
                             "-m", "4", "-o", "-", text],
                            capture_output=True, timeout=10, check=False)
    if result.returncode != 0 or not result.stdout.startswith(b"\x89PNG"):
        return None
    return result.stdout
