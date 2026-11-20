"""The phone link's self-signed certificate and the QR image."""

from __future__ import annotations

import hashlib
import io
import ipaddress
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
    (the ``phone`` extra), otherwise with the ``openssl`` command.  The
    certificate names the address it was made for; when the computer's address
    changes (DHCP, another network) it is made again, so the name always
    matches the page's address.  Phones then ask once more.
    """
    directory = Path(directory)
    cert, key = directory / "phone-cert.pem", directory / "phone-key.pem"
    marker = directory / "phone-cert.host"
    if cert.is_file() and key.is_file():
        if _covers(cert, marker, host, key=key):
            return cert, key
        for stale in (cert, key, marker):
            stale.unlink(missing_ok=True)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    made = _create_with_cryptography(cert, key, host)
    if not made:
        _create_with_openssl(cert, key, host)
    try:
        marker.write_text(host or "", encoding="utf-8")
    except OSError:
        pass
    return cert, key


def _covers(cert: Path, marker: Path, host: str,
            key: Path | None = None) -> bool:
    """Whether ``cert`` is valid, matches its key, is unexpired, and covers ``host``."""
    key_path = key if key is not None else cert.with_name("phone-key.pem")
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key_path)
    except (OSError, ssl.SSLError):
        return False

    try:
        import datetime

        from cryptography import x509

        parsed = x509.load_pem_x509_certificate(cert.read_bytes())
        now = datetime.datetime.now(datetime.timezone.utc)
        not_after = getattr(parsed, "not_valid_after_utc", None)
        if not_after is None:
            not_after = parsed.not_valid_after.replace(tzinfo=datetime.timezone.utc)
        not_before = getattr(parsed, "not_valid_before_utc", None)
        if not_before is None:
            not_before = parsed.not_valid_before.replace(tzinfo=datetime.timezone.utc)
        if not_after <= now or not_before > now + datetime.timedelta(minutes=5):
            return False
        if not host:
            return True
        try:
            san = parsed.extensions.get_extension_for_class(
                x509.SubjectAlternativeName).value
            names = {str(item.value) for item in san}
            return host in names
        except x509.ExtensionNotFound:
            pass
    except ImportError:
        openssl = shutil.which("openssl")
        if openssl is not None:
            try:
                check = subprocess.run(
                    [openssl, "x509", "-in", str(cert), "-checkend", "0", "-noout"],
                    capture_output=True, timeout=10, check=False)
                if check.returncode != 0:
                    return False
            except (OSError, subprocess.TimeoutExpired):
                pass
        if not host:
            return True
    except Exception:  # noqa: BLE001 - corrupt PEM / unreadable certificate
        return False

    if not host:
        return True
    try:
        return marker.read_text(encoding="utf-8").strip() == host
    except OSError:
        return True         # an older install: keep what phones already trust


def _create_with_openssl(cert: Path, key: Path, host: str) -> None:
    openssl = shutil.which("openssl")
    if openssl is None:
        raise RuntimeError("the phone link certificate needs the 'cryptography' "
                           "Python package or the openssl command")
    cert_tmp = cert.with_name(cert.name + ".tmp")
    key_tmp = key.with_name(key.name + ".tmp")
    # Pre-create the temporary private key file owner-only and run openssl
    # with umask 0077 so the private key is never world-readable.
    try:
        fd = os.open(key_tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        base = [openssl, "req", "-x509", "-newkey", "ec",
                "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                "-days", "3650", "-subj", "/CN=AnyCam phone link",
                "-keyout", str(key_tmp), "-out", str(cert_tmp)]
        # Browsers ignore the common name; the address must be a subjectAltName.
        names = ["DNS:localhost"]
        if host:
            try:
                ipaddress.ip_address(host)
                names.append(f"IP:{host}")
            except ValueError:
                names.append(f"DNS:{host}")
        result = subprocess.run(
            base + ["-addext", "subjectAltName=" + ",".join(names)],
            capture_output=True, text=True, timeout=30, check=False, umask=0o077)
        if result.returncode != 0:
            # OpenSSL older than 1.1.1 has no -addext: the page still works once
            # the phone accepts the warning, as before.
            result = subprocess.run(
                base, capture_output=True, text=True,
                timeout=30, check=False, umask=0o077)
        if result.returncode != 0 or not cert_tmp.is_file() or not key_tmp.is_file():
            raise RuntimeError("could not create the phone link certificate: "
                               + (result.stderr.strip() or "openssl failed"))
        os.chmod(key_tmp, 0o600)
        os.replace(key_tmp, key)
        os.replace(cert_tmp, cert)
    except BaseException:
        for stale in (key_tmp, cert_tmp):
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _create_with_cryptography(cert: Path, key: Path, host: str = "") -> bool:
    """EC P-256 self-signed certificate, 10 years; False without the package."""
    try:
        import datetime

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
    if host:
        try:
            alternative.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            alternative.append(x509.DNSName(host))
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
    pem_cert = certificate.public_bytes(serialization.Encoding.PEM)
    key_tmp = key.with_name(key.name + ".tmp")
    cert_tmp = cert.with_name(cert.name + ".tmp")
    # The key is written owner-only from the start and moved into place atomically.
    try:
        fd = os.open(key_tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(pem_key)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if fd >= 0:
                os.close(fd)
        fd = os.open(cert_tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(pem_cert)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if fd >= 0:
                os.close(fd)
        os.replace(key_tmp, key)
        os.replace(cert_tmp, cert)
    except BaseException:
        for stale in (key_tmp, cert_tmp):
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass
        raise
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
                             "-m", "4", "-o", "-"],
                            input=text.encode("utf-8"),
                            capture_output=True, timeout=10, check=False)
    if result.returncode != 0 or not result.stdout.startswith(b"\x89PNG"):
        return None
    return result.stdout
