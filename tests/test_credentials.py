"""The keyring wrapper: in-memory behaviour always, the real Secret Service only when asked.

The real-keyring tests write to whatever keyring answers on the session bus, so they
run only with ``ANYCAM_TEST_KEYRING=1`` (CI sets it inside a throwaway session).
"""
import os
import uuid

import pytest

from anycam import credentials


def test_a_stored_secret_is_found_again(memory_keyring):
    assert credentials.get_store().store("a", "label", "s3cret")
    assert credentials.get_store().lookup("a") == "s3cret"


def test_lookup_of_an_unknown_id_is_none(memory_keyring):
    assert credentials.get_store().lookup("nothing") is None


def test_remove_forgets_the_secret(memory_keyring):
    store = credentials.get_store()
    store.store("a", "label", "s3cret")
    assert store.remove("a")
    assert store.lookup("a") is None


def test_an_unavailable_keyring_reports_instead_of_raising(memory_keyring):
    memory_keyring.set_available(False)
    store = credentials.get_store()
    assert not credentials.available()
    assert store.store("a", "label", "x") is False
    assert store.lookup("a") is None
    assert store.remove("a") is False
    assert store.error


def test_set_store_none_goes_back_to_libsecret():
    credentials.set_store(None)
    assert isinstance(credentials.get_store(), credentials.LibsecretStore)


def test_libsecret_store_without_the_typelib_never_raises(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "gi.repository" and args and args[2] and "Secret" in args[2]:
            raise ImportError("no Secret")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    store = credentials.LibsecretStore()
    monkeypatch.undo()
    assert not store.available()
    assert store.store("a", "l", "x") is False
    assert store.lookup("a") is None
    assert store.remove("a") is False
    assert "libsecret" in store.error


real = pytest.mark.skipif(os.environ.get("ANYCAM_TEST_KEYRING") != "1",
                          reason="set ANYCAM_TEST_KEYRING=1 to use the real keyring")


@real
def test_the_real_keyring_round_trips():
    store = credentials.LibsecretStore()
    assert store.available(), store.error
    secret_id = f"test-{uuid.uuid4()}"
    try:
        assert store.store(secret_id, "AnyCam test", "pa55-ünï"), store.error
        assert store.lookup(secret_id) == "pa55-ünï"
    finally:
        store.remove(secret_id)
    assert store.lookup(secret_id) is None
