"""User store + password hashing for the login gate. Ported from cownting/auth.py.

Deliberately minimal: one `users` table in the same DuckDB file, passwords hashed
with stdlib scrypt (no extra dependency, no native build in the slim image), and
two roles. The session *cookie* is Starlette's SessionMiddleware in `api.py`;
this module owns only the credential store.

Two roles, not three (SPEC section 2): **admin** does everything — users,
classes, upload, delete, export, backup — and **annotator** labels crops, adds
classes and reads. A third tier was tried in the sibling project and every
question it answered ("who may download?") turned out to be a question about
admin. Resist adding one back: `is_admin` is the only gate the API needs, and a
role that is not in `ROLES` is rejected at write time so a typo cannot create a
user nobody can authorise.

The hash format is self-describing (`scrypt$N$r$p$salt$hash`), so raising the
work factors later does not invalidate the hashes already on disk.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from typing import Literal, Optional, TypedDict

import duckdb

Role = Literal["admin", "annotator"]
ROLES: tuple[Role, ...] = ("admin", "annotator")

# Env vars for the first-boot admin. Prefixed BIENENBLECH_ like everything else;
# read here at the point of use and never through Config, because config/ is
# committed and bind-mounted read-only.
ADMIN_USER_ENV = "BIENENBLECH_ADMIN_USER"
ADMIN_PASSWORD_ENV = "BIENENBLECH_ADMIN_PASSWORD"

# scrypt work factors (RFC 7914). N must be a power of two; these are the
# interactive-login defaults and hash in a few milliseconds. Raising N raises the
# login latency linearly — and because the parameters are stored inside every
# hash, old passwords keep verifying at the old cost until they are re-set.
_N, _R, _P, _DKLEN = 2**14, 8, 1, 32

# Usernames end up in URLs (DELETE /api/users/{username}) and in the `created_by`
# / `completed_by` provenance columns, so keep them boring and path-safe.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")


class UserRow(TypedDict):
    username: str
    role: Role
    created_at: Optional[str]


def is_admin(role: str | None) -> bool:
    """The only privilege predicate in the app. One definition, shared by the API
    dependency and any CLI check, so a route can never disagree with the CLI."""
    return role == "admin"


def valid_username(name: str) -> bool:
    return bool(_USERNAME_RE.fullmatch(name or ""))


# --------------------------------------------------------------------- hashing
def hash_password(password: str) -> str:
    """Salted scrypt hash, self-describing so `verify_password` never needs the
    parameters passed alongside it.

    Format: `scrypt$<N>$<r>$<p>$<salt_hex>$<hash_hex>`."""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of `password` against a `hash_password` string.

    Every malformed-input path returns False rather than raising: this runs on an
    unauthenticated route, and an exception here is a 500 that tells an attacker
    the stored row is unusual."""
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(hash_hex) // 2,
        )
    except (ValueError, TypeError, AttributeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


# ----------------------------------------------------------------------- store
def ensure_user_table(con: duckdb.DuckDBPyConnection) -> None:
    """Create the users table if absent. Idempotent, safe on every boot.

    Called by `db.init_db` as well, so app startup only has to make one call —
    but kept public and standalone because the CLI's user commands may run
    against a DB the server has never opened."""
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            username      TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'annotator',
            created_at    TIMESTAMP NOT NULL DEFAULT now()
        );
        """
    )


def get_user(con: duckdb.DuckDBPyConnection, username: str) -> Optional[dict]:
    """The full row including the hash. Internal-ish: never hand this to a route
    unfiltered, or the password hash ends up in a JSON response."""
    row = con.execute(
        "SELECT username, password_hash, role, created_at FROM users WHERE username = ?",
        [username],
    ).fetchone()
    if not row:
        return None
    return {
        "username": row[0],
        "password_hash": row[1],
        "role": row[2],
        "created_at": row[3].isoformat() if row[3] is not None else None,
    }


def user_exists(con: duckdb.DuckDBPyConnection, username: str) -> bool:
    return con.execute(
        "SELECT 1 FROM users WHERE username = ?", [username]
    ).fetchone() is not None


def list_users(con: duckdb.DuckDBPyConnection) -> list[UserRow]:
    """Every user, safe to return from GET /api/users — no hashes."""
    rows = con.execute(
        "SELECT username, role, created_at FROM users ORDER BY username"
    ).fetchall()
    return [
        {"username": u, "role": r, "created_at": c.isoformat() if c is not None else None}
        for u, r, c in rows
    ]


def count_admins(con: duckdb.DuckDBPyConnection) -> int:
    return int(con.execute(
        "SELECT count(*) FROM users WHERE role = 'admin'"
    ).fetchone()[0])


def create_user(
    con: duckdb.DuckDBPyConnection,
    username: str,
    password: str,
    role: Role = "annotator",
) -> UserRow:
    """Add a user. Raises ValueError on a bad name, an unknown role or a
    collision — the API maps that to 400."""
    if not valid_username(username):
        raise ValueError("username must be 1-32 chars of letters, digits, _ . or -")
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    if user_exists(con, username):
        raise ValueError(f"user {username!r} already exists")
    con.execute(
        "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, now())",
        [username, hash_password(password), role],
    )
    row = get_user(con, username)
    assert row is not None
    return {"username": row["username"], "role": row["role"], "created_at": row["created_at"]}


def verify_user(
    con: duckdb.DuckDBPyConnection, username: str, password: str
) -> Optional[UserRow]:
    """Return the user on a correct password, else None. No distinction between
    "no such user" and "wrong password" reaches the caller, on purpose."""
    u = get_user(con, username)
    if u and verify_password(password, u["password_hash"]):
        return {"username": u["username"], "role": u["role"], "created_at": u["created_at"]}
    return None


def set_password(con: duckdb.DuckDBPyConnection, username: str, password: str) -> None:
    if not user_exists(con, username):
        raise ValueError(f"unknown user {username!r}")
    con.execute(
        "UPDATE users SET password_hash = ? WHERE username = ?",
        [hash_password(password), username],
    )


def delete_user(con: duckdb.DuckDBPyConnection, username: str) -> None:
    """Remove a user. Refuses the last admin: the instance would become
    unadministrable with no recovery path short of editing the DuckDB file by
    hand, and this check is cheaper than that afternoon."""
    u = get_user(con, username)
    if not u:
        raise ValueError(f"unknown user {username!r}")
    if is_admin(u["role"]) and count_admins(con) <= 1:
        raise ValueError("cannot delete the last admin")
    con.execute("DELETE FROM users WHERE username = ?", [username])


def bootstrap_admin(con: duckdb.DuckDBPyConnection) -> Optional[str]:
    """Guarantee at least one admin so a fresh DB is reachable.

    Only fires when the table is EMPTY — never "when there is no admin". A
    deployment that deliberately has one operator account must not have a second
    one minted behind its back on the next restart, and the empty-table test is
    the one condition under which no human decision can be overwritten.

    Seeds from BIENENBLECH_ADMIN_USER / BIENENBLECH_ADMIN_PASSWORD (defaults
    `admin` / `admin`). Returns a human-readable warning when a default password
    was used, so the caller can print it, or None when nothing was created."""
    if list_users(con):
        return None
    username = (os.environ.get(ADMIN_USER_ENV) or "").strip() or "admin"
    password = (os.environ.get(ADMIN_PASSWORD_ENV) or "").strip()
    used_default = not password
    if not password:
        password = "admin"
    create_user(con, username, password, role="admin")
    if used_default:
        return (
            f"created bootstrap admin {username!r} with the DEFAULT password 'admin' — "
            f"change it on the Admin page, or set {ADMIN_PASSWORD_ENV} before first boot."
        )
    return f"created bootstrap admin {username!r} from the BIENENBLECH_ADMIN_* env vars."
