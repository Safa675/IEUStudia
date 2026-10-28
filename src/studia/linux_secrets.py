"""Linux: :func:`secrets_store.protect` and ``unprotect`` with a key kept in the desktop's keyring.

The counterpart of Windows DPAPI: a random 256-bit key lives in the Secret Service (GNOME Keyring, KWallet), which
unlocks when you log in, and ``secrets.bin`` is encrypted with it (AES-256-GCM). Another user of the PC, or someone
with only the file, can't read it. Without a keyring (a server, a session without D-Bus) nothing is stored: an
``OSError`` says why, and the password is asked for again next time instead of being written in plain text.
"""

from __future__ import annotations

import base64
import binascii
import functools
import os
from typing import Any, Optional

from .i18n import t

MAGIC = b"IEUS1"                                  # format version of what protect() returns
_ATTRS = {"application": "IEUStudia", "kind": "secrets-key"}
_LABEL = "IEUStudia (encrypts its saved sign-in)"


class KeyUnusable(OSError):
    """The keyring has IEUStudia's key, but not as one that can be used."""


def _decode(secret: bytes) -> Optional[bytes]:
    """The key in a keyring item: base64 text, or the 32 bytes themselves (as the first Linux version stored it)."""
    secret = bytes(secret)
    if len(secret) == 32:
        return secret
    try:
        key = base64.b64decode(secret.strip(), validate=True)
    except (ValueError, binascii.Error):
        return None
    return key if len(key) == 32 else None


def _collection_key(collection: Any) -> bytes:
    """The key from the keyring ``collection``, made and stored there the first time. Stored as text (base64), the
    secret's content type, as a keyring that keeps text as text would change raw bytes. An item that is there but
    can't be used is never replaced: what it encrypted (the sign-in, ChatGPT's) would be lost for good."""
    items = list(collection.search_items(_ATTRS))
    for item in items:
        key = _decode(item.get_secret())
        if key is not None:
            return key
    if items:
        raise KeyUnusable(t("The keyring has a key for IEUStudia that it can't use. Remove \"{label}\" in your keyring "
                            "app, then sign in again.", label=_LABEL))
    key = os.urandom(32)
    collection.create_item(_LABEL, _ATTRS, base64.b64encode(key), replace=False, content_type="text/plain")
    return key


@functools.lru_cache(maxsize=1)
def _key() -> bytes:
    try:
        import secretstorage
    except ImportError as exc:
        raise OSError(t("The secretstorage package is missing, so IEUStudia can't use the keyring.")) from exc
    try:
        conn = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(conn)
        if collection.is_locked():
            collection.unlock()                    # the desktop asks for the keyring password, if it's locked
        return _collection_key(collection)
    except KeyUnusable:
        raise
    except Exception as exc:  # noqa: BLE001 - D-Bus errors (no session bus) come in many types
        raise OSError(t("No keyring is available to keep IEUStudia's sign-in safe ({error}).", error=exc)) from exc


def protect(data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(_key()).encrypt(nonce, data, MAGIC)


def unprotect(data: bytes) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not data.startswith(MAGIC) or len(data) < len(MAGIC) + 12 + 16:
        raise OSError("not a file IEUStudia encrypted on Linux")
    nonce, body = data[len(MAGIC):len(MAGIC) + 12], data[len(MAGIC) + 12:]
    try:
        return AESGCM(_key()).decrypt(nonce, body, MAGIC)
    except InvalidTag as exc:
        raise OSError("the keyring's key doesn't open this file (another user's, or the key was removed)") from exc
