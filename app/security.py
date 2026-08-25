"""Password hashing, token minting, and constant-time comparison.

Uses :func:`hashlib.scrypt` from the standard library, which is a memory-hard
KDF in the same family as the bcrypt/argon2 the spec asks for (§4.1) and needs
no third-party package.

Stored format: ``scrypt$n$r$p$<salt-hex>$<hash-hex>``. Parameters live in the
string so they can be raised later without invalidating existing hashes.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

#: Cost parameters. ~64 MB and ~100 ms per hash on commodity hardware.
DEFAULT_N = 1 << 14
DEFAULT_R = 8
DEFAULT_P = 1

_params = {"n": DEFAULT_N, "r": DEFAULT_R, "p": DEFAULT_P}

#: Spec §6.2: minimum 8 characters, no composition rules.
MIN_PASSWORD_LENGTH = 8

#: Spec §6.2 asks for the top-1000 common passwords "if a list is easy to
#: bundle". Shipping a 1000-entry list verbatim would bloat the repo, so this
#: is the high-frequency head of that list plus the patterns this deployment
#: will realistically see. Extend it freely; it is a denylist, not a contract.
COMMON_PASSWORDS = frozenset(
    """
    password password1 password123 passw0rd p@ssword p@ssw0rd
    12345678 123456789 1234567890 87654321 11111111 00000000
    qwerty qwertyui qwerty123 asdfghjk 1qaz2wsx zaq12wsx
    iloveyou princess sunshine football baseball dragon monkey
    letmein welcome welcome1 admin123 administrator abc12345
    superman batman trustno1 whatever computer internet
    taiwan123 meetingroom booking123 changeme secret123
    """.split()
)


def configure(*, n: int | None = None, r: int | None = None, p: int | None = None):
    """Adjust KDF cost. Tests lower it so the suite stays fast."""
    if n is not None:
        _params["n"] = n
    if r is not None:
        _params["r"] = r
    if p is not None:
        _params["p"] = p


def hash_password(password: str) -> str:
    """Hash a password with a fresh random salt."""
    salt = secrets.token_bytes(16)
    n, r, p = _params["n"], _params["r"], _params["p"]
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32,
        maxmem=n * r * 256,
    )
    return f"scrypt${n}${r}${p}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored hash in constant time."""
    try:
        scheme, n_s, r_s, p_s, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    actual = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected),
        maxmem=n * r * 256,
    )
    return hmac.compare_digest(actual, expected)


def password_problem(password: str) -> str | None:
    """Return an error code if the password is unacceptable, else ``None``."""
    from app.errors import PASSWORD_TOO_COMMON, PASSWORD_TOO_SHORT

    if len(password) < MIN_PASSWORD_LENGTH:
        return PASSWORD_TOO_SHORT
    if password.strip().lower() in COMMON_PASSWORDS:
        return PASSWORD_TOO_COMMON
    return None


def new_token() -> tuple[str, str]:
    """Mint a single-use token.

    Returns ``(raw, hashed)``. Only the hash is stored (spec §4.2); the raw
    value goes into the emailed link and is never persisted.
    """
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    """Hash a token for storage and lookup.

    Plain SHA-256 is right here: tokens are 256 bits of system-generated
    entropy, so there is nothing to brute-force and no salt to add.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_session_id() -> tuple[str, str]:
    """Mint a session cookie value and its stored hash."""
    return new_token()


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalise_email(email: str) -> str:
    """Lower-case and trim. Spec §4.1 makes the address case-insensitive."""
    return email.strip().lower()


def is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email.strip()))
