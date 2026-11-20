"""Fernet and MultiFernet cipher helpers for secret key rotation."""

from __future__ import annotations

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
