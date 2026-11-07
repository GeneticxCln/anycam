"""The phone link's self-signed certificate and the QR image."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import socket
import ssl
import subprocess
from pathlib import Path


def ensure_certificate(directory: Path, host: str = "") -> tuple[Path, Path]:
    """Create (once) the self-signed certificate the phone page is served with.

    Phones only allow the camera on an HTTPS page.  There is no certificate
    authority for a LAN address, so this one is self-signed: the phone shows a
    warning once, and the desktop shows the fingerprint to compare.

    Made in-process with the ``cryptography`` package when it is installed
    (the ``phone`` extra), otherwise with the ``openssl`` command.
    """
    directory = Path(directory)
    cert, key = directory / "phone-cert.pem", directory / "phone-key.pem"
    if cert.is_file() and key.is_file():
        return cert, key
    directory.mkdir(parents=True, exist_ok=True)
    if _create_with_cryptography(cert, key, host):
        return cert, key
    openssl = shutil.which("openssl")
    if openssl is None:
        raise RuntimeError("the phone link certificate needs the 'cryptography' "
                           "Python package or the openssl command")
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


def _create_with_cryptography(cert: Path, key: Path, host: str = "") -> bool:
    """EC P-256 self-signed certificate, 10 years; False without the package."""
    try:
        import datetime
        import ipaddress

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError:
        return False
    private = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AnyCam phone link")])
    now = datetime.datetime.now(datetime.timezone.utc)
    alternative: list = [x509.DNSName("localhost")]
    try:
        if host:
            alternative.append(x509.IPAddress(ipaddress.ip_address(host)))
    except ValueError:
        pass
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(alternative), critical=False)
        .sign(private, hashes.SHA256())
    )
    pem_key = private.private_bytes(serialization.Encoding.PEM,
                                    serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
    # The key is written owner-only from the start, never world-readable.
    fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem_key)
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return True


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
