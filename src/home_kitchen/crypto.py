"""Fernet and MultiFernet cipher helpers for secret key rotation."""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from cryptography.fernet import Fernet, MultiFernet

FernetCipher = Fernet | MultiFernet


def split_fernet_keys(secret_value: str) -> list[str]:
    """Split a comma-separated secret value into non-empty stripped Fernet key strings."""
    if not isinstance(secret_value, str):
        raise TypeError("secret_value must be a string")
    return [part.strip() for part in secret_value.split(",") if part.strip()]


def build_multi_fernet(secret_value: str) -> MultiFernet:
    """Build a MultiFernet cipher from a single key or comma-separated Fernet keys.

    The first key is the primary key used for new encryptions; subsequent keys
    remain valid for decryption and key rotation.
    """
    keys = split_fernet_keys(secret_value)
    if not keys:
        raise ValueError("At least one Fernet key is required")
    return MultiFernet([Fernet(key.encode("utf-8")) for key in keys])


def resolve_connector_cipher(settings: Any) -> FernetCipher | None:
    """Resolve encryption cipher from connector or Oda key, or deterministic local_dev key."""
    key = getattr(settings, "connector_encryption_key", None) or getattr(
        settings, "oda_encryption_key", None
    )
    if key is not None:
        raw = key.get_secret_value() if hasattr(key, "get_secret_value") else str(key)
        return build_multi_fernet(raw)
    if getattr(settings, "local_dev", False):
        deterministic_dev_key = base64.urlsafe_b64encode(
            hashlib.sha256(b"kitchen-local-dev-connector-key-seed").digest()
        )
        return MultiFernet([Fernet(deterministic_dev_key)])
    return None


def resolve_oda_cipher(settings: Any) -> FernetCipher | None:
    """Resolve Oda OAuth/cart encryption cipher from settings."""
    key = getattr(settings, "oda_encryption_key", None)
    if key is None:
        return None
    raw = key.get_secret_value() if hasattr(key, "get_secret_value") else str(key)
    return build_multi_fernet(raw)
