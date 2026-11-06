"""Web app users: the password hash (and its twin in web/server/utils/auth.ts), creating a user, the add-user command, and the three doors in the web tables' row level security."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from conftest import TENANT
from xerlane import users
from xerlane.db import tenant_tx
from xerlane.users import UserError

ROOT = Path(__file__).resolve().parent.parent
# A hash made here with a fixed salt. web/test/auth.test.ts verifies the same string with Node's scrypt, so a change to either side shows up in both suites.
VECTOR = "scrypt$16384$8$1$AAECAwQFBgcICQoLDA0ODw$11kKyiyYAc8G7rp3KmncMc44YlkdllIqxOa7pq0fMaU"
PASSWORD = "correct horse battery staple"


def test_the_shared_vector_verifies_and_the_format_is_what_the_web_app_reads():
    assert users.verify_password(PASSWORD, VECTOR)
    assert users.hash_password(PASSWORD, salt=bytes(range(16))) == VECTOR
    kind, n, r, p, salt, digest = VECTOR.split("$")
    assert (kind, n, r, p) == ("scrypt", "16384", "8", "1") and "=" not in salt + digest and len(users._unb64(digest)) == 32


def test_wrong_passwords_and_damaged_hashes_never_verify():
    assert not users.verify_password(PASSWORD + "x", VECTOR) and not users.verify_password("", VECTOR) and not users.verify_password(PASSWORD.upper(), VECTOR)
    for bad in ("", "scrypt", "scrypt$1$2$3$4", "bcrypt$16384$8$1$AAECAwQFBgcICQoLDA0ODw$11kKyiyYAc8G7rp3KmncMc44YlkdllIqxOa7pq0fMaU", "scrypt$x$8$1$AA$AA", "scrypt$16384$8$1$!!$!!",
                VECTOR[:-4], VECTOR.replace("$16384$", "$0$"), VECTOR + "$extra"):
        assert users.verify_password(PASSWORD, bad) is False, bad


def test_every_hash_has_its_own_salt_and_unicode_passwords_round_trip():
    a, b = users.hash_password(PASSWORD), users.hash_password(PASSWORD)
    assert a != b and users.verify_password(PASSWORD, a) and users.verify_password(PASSWORD, b)
    pw = "pässwörd with spaces 密码 \U0001f511"
    assert users.verify_password(pw, users.hash_password(pw)) and not users.verify_password(pw[:-1], users.hash_password(pw))


def test_new_user_checks():
    assert users.check_new_user("  Ann@Example.COM ", PASSWORD, "viewer") == "ann@example.com"
    for email in ("", "no-at", "a@", "@b", "a b@c.d", "a@b@c", "x" * 250 + "@y.zz"):
        with pytest.raises(UserError, match="email"):
            users.check_new_user(email, PASSWORD, "viewer")
    with pytest.raises(UserError, match="role"):
        users.check_new_user("a@b.c", PASSWORD, "owner")
    with pytest.raises(UserError, match="at least 12"):
        users.check_new_user("a@b.c", "x" * 11, "viewer")
    users.check_new_user("a@b.c", "x" * 12, "viewer")
    users.check_new_user("a@b.c", "x" * 256, "admin")
    with pytest.raises(UserError, match="at most 256"):
        users.check_new_user("a@b.c", "x" * 257, "viewer")


@pytest.mark.db
def test_add_user_stores_a_lower_case_email_and_a_hash_not_the_password(conn, admin):
    uid = users.add_user(conn, TENANT, " Ann@Example.com ", PASSWORD, "admin")
    row = admin.execute("select tenant_id, user_id, email, role, disabled, password_hash from app_user").fetchone()
    assert row[:5] == (TENANT, uid, "ann@example.com", "admin", False) and PASSWORD not in row[5] and users.verify_password(PASSWORD, row[5])


@pytest.mark.db
def test_an_email_belongs_to_one_user_in_the_whole_system(conn):
    users.add_user(conn, TENANT, "ann@example.com", PASSWORD)
    with pytest.raises(UserError, match="already exists"):
        users.add_user(conn, "globex", "ANN@example.com", PASSWORD)
    with pytest.raises(UserError, match="already exists"):
        users.add_user(conn, TENANT, "ann@example.com", PASSWORD)


@pytest.mark.db
def test_tenants_see_only_their_own_users_and_nobody_without_a_tenant_sees_any(conn):
    users.add_user(conn, TENANT, "ann@example.com", PASSWORD)
    users.add_user(conn, "globex", "bob@example.com", PASSWORD)
    for tenant, want in ((TENANT, ["ann@example.com"]), ("globex", ["bob@example.com"]), ("third", [])):
        with tenant_tx(conn, tenant) as cur:
            cur.execute("select email from app_user")
            assert [r[0] for r in cur.fetchall()] == want
    assert conn.execute("select count(*) from app_user").fetchone()[0] == 0
    conn.rollback()


@pytest.mark.db
def test_a_tenant_cannot_write_a_user_into_another_tenant(conn):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with tenant_tx(conn, TENANT) as cur:
            cur.execute("insert into app_user (tenant_id, email, password_hash, role) values ('globex', 'x@y.zz', %s, 'viewer')", (VECTOR,))


@pytest.mark.db
def test_the_login_door_shows_exactly_the_user_being_tried_and_changes_nothing(conn, admin):
    users.add_user(conn, TENANT, "ann@example.com", PASSWORD)
    users.add_user(conn, "globex", "bob@example.com", PASSWORD)
    with conn.transaction():
        conn.execute("select set_config('app.login_email', 'bob@example.com', true)")
        assert [r[0] for r in conn.execute("select email from app_user").fetchall()] == ["bob@example.com"]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("update app_user set role = 'admin'")
            conn.execute("insert into app_user (tenant_id, email, password_hash, role) values ('globex', 'z@y.zz', %s, 'viewer')", (VECTOR,))
    assert admin.execute("select role from app_user where email = 'bob@example.com'").fetchone()[0] == "viewer"


@pytest.mark.db
def test_the_session_door_shows_exactly_the_session_being_checked(conn, admin):
    ann = users.add_user(conn, TENANT, "ann@example.com", PASSWORD)
    bob = users.add_user(conn, "globex", "bob@example.com", PASSWORD)
    for tenant, uid, h in ((TENANT, ann, "a" * 64), ("globex", bob, "b" * 64)):
        with tenant_tx(conn, tenant) as cur:
            cur.execute("insert into app_session (tenant_id, session_hash, user_id, expires_at) values (%s, %s, %s, now() + interval '1 hour')", (tenant, h, uid))
    with conn.transaction():
        conn.execute("select set_config('app.session_hash', %s, true)", ("b" * 64,))
        assert [r[0] for r in conn.execute("select tenant_id from app_session").fetchall()] == ["globex"]
    assert conn.execute("select count(*) from app_session").fetchone()[0] == 0
    conn.rollback()
    assert admin.execute("select count(*) from app_session").fetchone()[0] == 2


@pytest.mark.db
def test_deleting_a_user_deletes_their_sessions(conn, admin):
    ann = users.add_user(conn, TENANT, "ann@example.com", PASSWORD)
    with tenant_tx(conn, TENANT) as cur:
        cur.execute("insert into app_session (tenant_id, session_hash, user_id, expires_at) values (%s, %s, %s, now() + interval '1 hour')", (TENANT, "c" * 64, ann))
    admin.execute("delete from app_user")
    assert admin.execute("select count(*) from app_session").fetchone()[0] == 0


@pytest.mark.db
def test_login_attempts_are_visible_only_for_the_email_being_tried(conn, admin):
    admin.execute("insert into login_attempt (email) values ('a@x.zz'), ('b@x.zz'), ('b@x.zz')")
    with conn.transaction():
        conn.execute("select set_config('app.login_email', 'b@x.zz', true)")
        assert conn.execute("select count(*) from login_attempt").fetchone()[0] == 2
    assert conn.execute("select count(*) from login_attempt").fetchone()[0] == 0
    conn.rollback()


def test_session_hash_must_be_a_sha256_hex_length(admin):
    admin.execute("insert into app_user (tenant_id, email, password_hash, role) values ('t', 'a@b.cc', %s, 'viewer')", (VECTOR,))
    uid = admin.execute("select user_id from app_user").fetchone()[0]
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("insert into app_session (tenant_id, session_hash, user_id, expires_at) values ('t', 'short', %s, now())", (uid,))


@pytest.mark.parametrize("email,hash_,role", [("Upper@x.cc", VECTOR, "viewer"), ("no-at", VECTOR, "viewer"), ("a@b.cc", "plain-password", "viewer"), ("a@b.cc", VECTOR, "owner")])
def test_the_table_refuses_what_the_code_would_never_write(admin, email, hash_, role):
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("insert into app_user (tenant_id, email, password_hash, role) values ('t', %s, %s, %s)", (email, hash_, role))


@pytest.mark.db
def test_add_user_command(clean):
    env = {**os.environ, "XERLANE_NEW_PASSWORD": PASSWORD}
    cmd = [sys.executable, "-m", "xerlane", "add-user", "--tenant", TENANT, "--email", "Cli@Example.com", "--role", "admin"]
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    assert p.returncode == 0 and "created: cli@example.com (admin) for tenant acme" in p.stdout and PASSWORD not in p.stdout + p.stderr
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    assert p.returncode == 2 and "already exists" in p.stderr
    p = subprocess.run(cmd[:-1] + ["owner"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    assert p.returncode == 2 and "invalid choice" in p.stderr
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env={**env, "XERLANE_NEW_PASSWORD": "short"}, timeout=60)
    assert p.returncode == 2 and "at least 12" in p.stderr
