"""Secrets (stream keys, passphrases, camera passwords) in the desktop keyring.

The freedesktop Secret Service (gnome-keyring, KWallet's ksecretd, KeePassXC) is
reached through libsecret's ``gi`` bindings, so no extra PyPI package is needed.
Every store call here is safe to make from UI code: none raises.  ``store`` and
``remove`` return whether they worked, ``lookup`` returns ``None`` when there is
nothing to give (nothing stored, a locked keyring that was not unlocked in time,
or no keyring at all), and ``error`` says why the last call failed.

The module is not called ``secrets`` so it cannot be mistaken for the standard
library's.
"""
from __future__ import annotations

import re
import sys
import threading
import uuid
from typing import Protocol

SCHEMA_NAME = "io.github.geneticxcln.AnyCam.Secret"
ATTRIBUTE = "anycam-secret-id"
# A locked keyring shows its own unlock prompt; wait this long for the answer.
PROMPT_TIMEOUT_S = 30.0
PROBE_TIMEOUT_S = 2.0


class Store(Protocol):
    error: str

    def available(self) -> bool: ...
    def store(self, secret_id: str, label: str, secret: str) -> bool: ...
    def lookup(self, secret_id: str) -> str | None: ...
    def remove(self, secret_id: str) -> bool: ...


class MemoryStore:
    """A keyring that lives in this process only (tests, and a stand-in)."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.labels: dict[str, str] = {}
        self.error = ""
        self._available = True

    def set_available(self, value: bool) -> None:
        self._available = value

    def available(self) -> bool:
        return self._available

    def store(self, secret_id: str, label: str, secret: str) -> bool:
        if not self._available:
            self.error = "no keyring is available"
            return False
        self.values[secret_id] = secret
        self.labels[secret_id] = label
        return True

    def lookup(self, secret_id: str) -> str | None:
        if not self._available:
            self.error = "no keyring is available"
            return None
        return self.values.get(secret_id)

    def remove(self, secret_id: str) -> bool:
        if not self._available:
            self.error = "no keyring is available"
            return False
        self.values.pop(secret_id, None)
        self.labels.pop(secret_id, None)
        return True


class LibsecretStore:
    """The Secret Service over libsecret (``gir1.2-secret-1``)."""

    def __init__(self) -> None:
        self.error = ""
        self._secret = None
        self._schema = None
        self._gio = None
        try:
            import gi

            gi.require_version("Secret", "1")
            from gi.repository import Gio, Secret

            self._gio = Gio
            self._secret = Secret
            self._schema = Secret.Schema.new(
                SCHEMA_NAME, Secret.SchemaFlags.NONE,
                {ATTRIBUTE: Secret.SchemaAttributeType.STRING})
        except (ImportError, ValueError) as exc:
            self.error = f"libsecret is not installed ({exc})"

    # ------------------------------------------------------------- plumbing
    def available(self) -> bool:
        """Whether a Secret Service answers on the session bus (or can start)."""
        if self._secret is None:
            return False
        Gio = self._gio
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            for method in ("NameHasOwner", "ListActivatableNames"):
                reply = bus.call_sync(
                    "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                    method, self._gio_args(method), None, Gio.DBusCallFlags.NONE,
                    int(PROBE_TIMEOUT_S * 1000), None)
                value = reply.unpack()[0]
                if method == "NameHasOwner" and value:
                    return True
                if method == "ListActivatableNames" and "org.freedesktop.secrets" in value:
                    return True
        except Exception as exc:  # noqa: BLE001 - no session bus, no keyring
            self.error = f"no session bus ({exc})"
            return False
        self.error = "no Secret Service (gnome-keyring, KWallet or KeePassXC) answers"
        return False

    def _gio_args(self, method: str):
        from gi.repository import GLib

        if method == "NameHasOwner":
            return GLib.Variant("(s)", ("org.freedesktop.secrets",))
        return None

    def _run(self, call):
        """Run a blocking libsecret call, cancelling it after the prompt timeout."""
        if self._secret is None:
            return None
        cancellable = self._gio.Cancellable()
        timer = threading.Timer(PROMPT_TIMEOUT_S, cancellable.cancel)
        timer.daemon = True
        timer.start()
        try:
            self.error = ""
            return call(cancellable)
        except Exception as exc:  # noqa: BLE001 - GLib.Error and friends
            self.error = str(exc) or type(exc).__name__
            return None
        finally:
            timer.cancel()

    # ------------------------------------------------------------------ API
    def store(self, secret_id: str, label: str, secret: str) -> bool:
        Secret = self._secret
        if Secret is None:
            return False
        done = self._run(lambda c: Secret.password_store_sync(
            self._schema, {ATTRIBUTE: secret_id}, Secret.COLLECTION_DEFAULT, label, secret, c))
        return bool(done)

    def lookup(self, secret_id: str) -> str | None:
        Secret = self._secret
        if Secret is None:
            return None
        return self._run(lambda c: Secret.password_lookup_sync(
            self._schema, {ATTRIBUTE: secret_id}, c))

    def remove(self, secret_id: str) -> bool:
        Secret = self._secret
        if Secret is None:
            return False
        self._run(lambda c: Secret.password_clear_sync(
            self._schema, {ATTRIBUTE: secret_id}, c))
        return not self.error


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = LibsecretStore()
    return _store


def set_store(store: Store | None) -> None:
    """Replace the keyring (tests); ``None`` goes back to libsecret."""
    global _store
    _store = store


def available() -> bool:
    return get_store().available()


# ------------------------------------------------------- secrets inside URLs
#
# A saved project never holds a stream key, passphrase or camera password: the
# URL keeps a reference, ``rtmp://host/app/${secret:<id>}``, and the value lives
# in the keyring.  In memory a project always holds the real URL, so the engine
# needs no change: references are resolved when a file is read (``reveal``) and
# made when one is written (``protect``).

REF = re.compile(r"\$\{secret:([A-Za-z0-9-]{8,64})\}")

_USERINFO_PASSWORD = re.compile(r"(?P<pre>\b[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]*:)(?P<value>[^/\s@]+)(?=@)")
_QUERY_SECRET = re.compile(r"(?P<pre>[?&](?:passphrase|streamid|key)=)(?P<value>[^&#\s]+)", re.IGNORECASE)
_STREAM_KEY = re.compile(r"(?P<pre>^rtmps?://[^/\s]+(?:/[^/\s?#]+)+/)(?P<value>[^/\s?#]+)(?=[?#]|$)", re.IGNORECASE)

# secret value -> id, so saving the same secret again reuses its keyring entry.
_known_ids: dict[str, str] = {}
_warned_inline = False


def has_reference(text: str) -> bool:
    return bool(REF.search(text or ""))


def _spans(text: str, stream: bool):
    patterns = [_USERINFO_PASSWORD, _QUERY_SECRET] + ([_STREAM_KEY] if stream else [])
    found = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            found.append(match.span("value"))
    return sorted(found)


def contains_secret(text: str, stream: bool = False) -> bool:
    """Whether ``text`` carries an inline secret (one that is not yet a reference)."""
    return any(not REF.fullmatch(text[a:b]) for a, b in _spans(text or "", stream))


def reveal(text: str) -> str:
    """Put the real secrets back in place of references; unknown ones stay as written."""
    if not text or "${secret:" not in text:
        return text

    def swap(match):
        value = get_store().lookup(match.group(1))
        if value is None:
            return match.group(0)
        _known_ids.setdefault(value, match.group(1))
        return value

    return REF.sub(swap, text)


def protect(text: str, stream: bool = False, label: str = "AnyCam secret") -> str:
    """``text`` with its inline secrets moved to the keyring and replaced by references.

    With no working keyring the secret stays where it is (the project file is
    owner-only), and one line says so per run.
    """
    global _warned_inline
    if not text:
        return text
    pieces = []
    last = 0
    store = get_store()
    for start, end in _spans(text, stream):
        value = text[start:end]
        if REF.fullmatch(value):
            continue
        secret_id = _known_ids.get(value)
        if secret_id is not None and store.lookup(secret_id) != value:
            secret_id = None  # the entry was deleted or changed behind our back
        if secret_id is None:
            new_id = _known_ids.get(value) or str(uuid.uuid4())
            if not store.store(new_id, label, value):
                if not _warned_inline:
                    _warned_inline = True
                    print("[credentials] no working keyring: stream keys and passwords stay in the "
                          f"project file ({store.error or 'not available'})", file=sys.stderr, flush=True)
                continue
            _known_ids[value] = secret_id = new_id
        pieces.append(text[last:start])
        pieces.append("${secret:" + secret_id + "}")
        last = end
    pieces.append(text[last:])
    return "".join(pieces)


def known_secrets() -> list[str]:
    """Every secret value this run has stored or revealed (for masking in the log)."""
    return list(_known_ids)
