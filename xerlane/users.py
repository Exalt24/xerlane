"""Web app users: password hashing the web server can check, and creating a user for a tenant.

The hash is scrypt, stored as  scrypt$N$r$p$salt$hash  with salt and hash in unpadded URL-safe base64. The web app (web/server/utils/auth.ts) checks the same format with
Node's crypto.scrypt; tests/test_users.py holds a hash made here that web/test/auth.test.ts verifies, so the two sides cannot drift apart unseen.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re

import psycopg

from .db import tenant_tx

N, R, P, DKLEN = 16384, 8, 1, 32
MIN_PASSWORD = 12
MAX_PASSWORD = 256
ROLES = ("viewer", "admin")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+")


class UserError(ValueError):
    """The user cannot be created as asked; the message says why."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = os.urandom(16) if salt is None else salt
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=N, r=R, p=P, dklen=DKLEN, maxmem=64 * 1024 * 1024)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, want = _unb64(parts[4]), _unb64(parts[5])
        if len(want) != DKLEN:
            return False                                    # scrypt output of a shorter length is a prefix of the longer one, so a cut-off hash would accept a short guess
        got = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(want), maxmem=128 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


def check_new_user(email: str, password: str, role: str) -> str:
    """The email in the form it is stored (trimmed, lower case), or UserError."""
    email = email.strip().lower()
    if not EMAIL_RE.fullmatch(email) or len(email) > 254:
        raise UserError(f"{email!r} is not an email address")
    if role not in ROLES:
        raise UserError(f"role must be one of {', '.join(ROLES)}")
    if len(password) < MIN_PASSWORD:
        raise UserError(f"the password must have at least {MIN_PASSWORD} characters")
    if len(password) > MAX_PASSWORD:
        raise UserError(f"the password must have at most {MAX_PASSWORD} characters")
    return email


def add_user(conn: psycopg.Connection, tenant: str, email: str, password: str, role: str = "viewer") -> int:
    """Create a user of `tenant` and return the user id. An email belongs to one user in the whole system, because it is what a login starts from."""
    email = check_new_user(email, password, role)
    try:
        with tenant_tx(conn, tenant) as cur:
            cur.execute("INSERT INTO app_user (tenant_id, email, password_hash, role) VALUES (%s, %s, %s, %s) RETURNING user_id", (tenant, email, hash_password(password), role))
            return cur.fetchone()[0]
    except psycopg.errors.UniqueViolation:
        raise UserError(f"a user with the email {email} already exists") from None
