"""Password and token primitives.

Passwords use argon2id.  Refresh/device secrets are stored as SHA-256 digests
so a database leak does not expose usable tokens.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Return an argon2id digest for ``password``."""
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time-ish verification that never raises on bad input."""
    if not password or not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the stored digest should be upgraded on next login."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def hash_token(raw_token: str) -> str:
    """Stable digest used as the lookup key for refresh tokens."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def new_token(nbytes: int = 48) -> str:
    """Cryptographically random URL-safe token."""
    return secrets.token_urlsafe(nbytes)


def new_csrf_token() -> str:
    """Random value embedded in the JWT ``csrf`` claim."""
    return secrets.token_urlsafe(32)


def new_device_comm_key(digits: int = 6) -> str:
    """A numeric communication key a device keypad can actually accept.

    ZKTeco terminals are configured from a numeric keypad, and the "Comm Key"
    (some firmware calls it the communication password) is a *number* — usually
    limited to a handful of digits. A long alphanumeric token cannot be typed
    in, so the shared secret for ADMS push devices must be numeric.

    Leading zeros are preserved because the value is returned as a string.
    """
    return "".join(secrets.choice("0123456789") for _ in range(digits))


def hash_device_secret(secret: str, *, salt: str = "") -> str:
    """Hash a biometric device shared secret (salted with the device serial)."""
    digest = hashlib.sha256()
    digest.update(salt.encode("utf-8"))
    digest.update(b"|")
    digest.update(secret.encode("utf-8"))
    return digest.hexdigest()


def verify_device_secret(secret: str, secret_hash: str, *, salt: str = "") -> bool:
    """Timing-safe comparison of a device secret against its stored digest."""
    if not secret or not secret_hash:
        return False
    return hmac.compare_digest(hash_device_secret(secret, salt=salt), secret_hash)
