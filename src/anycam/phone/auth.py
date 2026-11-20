"""Pairing tokens (one per QR code) and the registry of paired phones."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

TOKEN_TTL_S = 300.0


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def clean_name(name: object) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", "", str(name or "")).strip()
    return text[:40] or "Phone"


class Pairing:
    """One-time QR tokens and paired devices (secrets stored hashed)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._tokens: dict[str, float] = {}
        self._devices: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): value for key, value in data.items()
                if isinstance(value, dict) and isinstance(value.get("secret"), str)}

    def _save(self, devices: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                json.dump(devices, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def new_token(self) -> str:
        """A token for one QR code: single use, valid for five minutes."""
        token = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._lock:
            self._tokens = {key: expiry for key, expiry in self._tokens.items()
                            if expiry > now}
            self._tokens[token] = now + TOKEN_TTL_S
        return token

    def pair(self, token: str, name: object) -> tuple[str, str] | None:
        """Consume ``token``; return ``(device_id, secret)`` or None."""
        with self._lock:
            key = str(token or "")
            expiry = self._tokens.get(key)
            if expiry is None or expiry < time.monotonic():
                self._tokens.pop(key, None)
                return None
            device = "phone-" + secrets.token_hex(4)
            while device in self._devices:
                device = "phone-" + secrets.token_hex(4)
            secret = secrets.token_urlsafe(32)
            updated = dict(self._devices)
            updated[device] = {"name": clean_name(name),
                               "secret": _hash(secret),
                               "paired": int(time.time())}
            self._save(updated)
            self._tokens.pop(key, None)
            self._devices = updated
        return device, secret

    def check(self, bearer: str) -> str | None:
        """The device id for a ``<device>.<secret>`` credential, or None."""
        device, _, secret = str(bearer or "").partition(".")
        with self._lock:
            record = self._devices.get(device)
        if record is None or not secret:
            return None
        if not hmac.compare_digest(_hash(secret), record["secret"]):
            return None
        return device

    def name(self, device: str) -> str:
        with self._lock:
            record = self._devices.get(device) or {}
        return str(record.get("name") or device)

    def devices(self) -> dict[str, str]:
        with self._lock:
            return {key: str(value.get("name") or key)
                    for key, value in self._devices.items()}

    def forget(self, device: str) -> bool:
        with self._lock:
            if device not in self._devices:
                return False
            updated = dict(self._devices)
            del updated[device]
            self._save(updated)
            self._devices = updated
        return True
