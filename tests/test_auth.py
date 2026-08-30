"""The login gate and the admin/poweruser split (SPEC sections 2 and 5).

The second role is named 'poweruser'. SPEC section 2 called it 'annotator';
that is amended (recorded here, because the SPEC is frozen): same two-role
model, admin unchanged, but powerusers may additionally upload frames. The
role-rename section near the bottom of this file pins the amendment: the API
speaks only the new name, refuses the old one loudly, and a store written by
an older build is migrated at boot.

SPEC section 5: "every route except `/api/health` and `/api/login` requires a
session". `api.PUBLIC_API` widens that by two — `/api/logout`, which must be
callable with a dead session, and `/api/me`, which answers 401 itself so the SPA
can render a login screen instead of the browser inventing an error. Everything
else is gated by an app-level dependency, which is the shape that matters: the
failure mode of an opt-in gate is a route somebody adds next year and forgets to
protect. The first test in this file is what would catch that.

The second half of the file covers the login Discord ping. Same safety rule as
`test_backup.py`: **no test in this module may ever reach a real webhook** — the
autouse `_no_network` fixture unsets `BIENENBLECH_DISCORD_WEBHOOK` and replaces
both `api._login_poster` and `urllib.request.urlopen` with functions that fail
the test, and every ping test injects a recording poster and an inline-running
thread shim instead. A post cannot be un-posted.
"""
from __future__ import annotations

import os
import urllib.request
from pathlib import Path
from typing import Any, Callable

import duckdb
import pytest
from fastapi.testclient import TestClient

from bienenblech import api

from conftest import (
    ADMIN_PASSWORD,
    ADMIN_USER,
    POWERUSER_PASSWORD,
    POWERUSER_USER,
    frame_bytes,
)

# One route per resource family. Not exhaustive by design — the gate is
# app-wide, so a hole in it shows up here whatever the route.
PROTECTED = [
    ("GET", "/api/users"),
    ("GET", "/api/images"),
    ("GET", "/api/classes"),
    ("GET", "/api/stats"),
    ("GET", "/api/crops/next"),
    ("GET", "/api/backup/status"),
    ("GET", "/api/export/yolo"),
    ("POST", "/api/masks"),
    ("POST", "/api/classes"),
]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_protected_routes_refuse_anonymous(client: TestClient, method: str, path: str):
    """No session, no data. 401, and never a 200 or a 500."""
    resp = client.request(method, path, json={})
    assert resp.status_code == 401, f"{method} {path} -> {resp.status_code} {resp.text}"
    assert resp.json()["detail"] == "login required"


def test_health_is_public(client: TestClient):
    """`/api/health` is the container's healthcheck; it runs before anyone can
    log in, so gating it would make the container permanently unhealthy."""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["schema_version"] is not None


def test_login_is_public(client: TestClient):
    """Wrong credentials must be answered by the login handler (401 "invalid
    username or password"), not by the gate — otherwise nobody could ever sign
    in at all."""
    resp = client.post(
        "/api/login", json={"username": ADMIN_USER, "password": "wrong"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid username or password"


def test_me_answers_401_rather_than_being_gated(client: TestClient):
    """The SPA probes `/api/me` on load to decide between the app and the login
    screen, so it has to be reachable and answer 401 in the same JSON shape."""
    resp = client.get("/api/me")
    assert resp.status_code == 401
    assert "detail" in resp.json()


def test_login_success_returns_identity(client: TestClient):
    resp = client.post(
        "/api/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD}
    )
    assert resp.status_code == 200
    assert resp.json() == {"username": ADMIN_USER, "role": "admin", "age_enabled": True}


def test_login_failure_does_not_leak_whether_the_user_exists(client: TestClient):
    """`auth.verify_user` deliberately collapses "no such user" and "wrong
    password" into one None, so the two must be indistinguishable on the wire."""
    unknown = client.post(
        "/api/login", json={"username": "nobody_here", "password": "x"}
    )
    wrong = client.post(
        "/api/login", json={"username": ADMIN_USER, "password": "x"}
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_session_persists_across_requests(admin: TestClient):
    """The session cookie is the whole auth story; if it did not survive the
    next request, every user would be signed out mid-crop."""
    for _ in range(3):
        resp = admin.get("/api/me")
        assert resp.status_code == 200
        assert resp.json()["username"] == ADMIN_USER


def test_logout_clears_the_session(admin: TestClient):
    assert admin.post("/api/logout").status_code == 200
    assert admin.get("/api/me").status_code == 401
    assert admin.get("/api/images").status_code == 401


def test_login_rotates_the_session(client: TestClient):
    """`login` clears the session before writing to it, so a session fixed by an
    attacker before the login cannot survive into the authenticated one."""
    client.cookies.set("session", "attacker-planted-value")
    resp = client.post(
        "/api/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD}
    )
    assert resp.status_code == 200
    assert client.get("/api/me").json()["username"] == ADMIN_USER


# ------------------------------------------------------------------ admin gates
def test_poweruser_cannot_list_users(poweruser: TestClient):
    resp = poweruser.get("/api/users")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "admin only"


def test_poweruser_cannot_create_users(poweruser: TestClient):
    """Otherwise the poweruser/admin split is decorative: anyone could mint
    themselves an admin account."""
    resp = poweruser.post(
        "/api/users", json={"username": "sneaky", "password": "pw", "role": "admin"}
    )
    assert resp.status_code == 403


def test_poweruser_cannot_delete_users(poweruser: TestClient):
    resp = poweruser.delete(f"/api/users/{ADMIN_USER}")
    assert resp.status_code == 403


def test_poweruser_may_upload_images(poweruser: TestClient):
    """Upload is open to powerusers — the amendment that motivated the role
    rename (SPEC section 2 as amended; the SPEC itself is frozen). Every
    signed-in user is now admin or poweruser, so `POST /api/images` is open to
    any session, and the whole pipeline behind the route — derivative, tiling,
    crop rows — must run for them, not just the gate. Deletion stays a decision
    about other people's work, so it remains admin-only, pinned right below."""
    resp = poweruser.post(
        "/api/images",
        files={"file": ("frame.png", frame_bytes(640, 480, seed=7), "image/png")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["duplicates"] == []
    summary = body["images"][0]
    assert summary["n_crops"] == 1, summary  # 640x480 fits in a single 640px tile
    crops = poweruser.get(f"/api/images/{summary['image_id']}").json()["crops"]
    assert len(crops) == 1 and crops[0]["status"] == "open"


def test_poweruser_cannot_delete_images(poweruser: TestClient, image: dict):
    """The one hard delete in the schema (SPEC section 4). It takes the masks
    with it, so it stays behind the admin gate — upload rights do not imply
    delete rights."""
    resp = poweruser.delete(f"/api/images/{image['image_id']}?force=true")
    assert resp.status_code == 403


def test_poweruser_cannot_export(poweruser: TestClient):
    resp = poweruser.get("/api/export/yolo")
    assert resp.status_code == 403


def test_poweruser_cannot_trigger_a_backup(poweruser: TestClient):
    """403 has to come from the gate, before `backup.run_backup` is imported or
    a zip is written."""
    resp = poweruser.post("/api/backup/run", json={})
    assert resp.status_code == 403


def test_admin_may_list_users(admin: TestClient, poweruser: TestClient):
    resp = admin.get("/api/users")
    assert resp.status_code == 200
    names = {u["username"] for u in resp.json()}
    assert names == {ADMIN_USER, POWERUSER_USER}
    assert all("password_hash" not in u for u in resp.json()), (
        "GET /api/users must never carry a password hash"
    )


# -------------------------------------------------------------------- last admin
def test_cannot_delete_the_last_admin(admin: TestClient, poweruser: TestClient):
    """An instance with no admin can never be administered again, and there is
    no shell in the container to fix it from. The poweruser in the fixture is
    there to prove the check counts admins, not users."""
    resp = admin.delete(f"/api/users/{ADMIN_USER}")
    assert resp.status_code == 400
    assert "last admin" in resp.json()["detail"]
    assert admin.get("/api/me").status_code == 200


def test_an_admin_may_be_deleted_while_another_remains(admin: TestClient):
    """The guard is "the last admin", not "any admin" — two admins must still be
    reducible to one, or the check would make every account permanent."""
    assert admin.post(
        "/api/users",
        json={"username": "second_admin", "password": "pw", "role": "admin"},
    ).status_code == 200

    assert admin.delete("/api/users/second_admin").status_code == 200
    names = {u["username"] for u in admin.get("/api/users").json()}
    assert names == {ADMIN_USER}
    # And now that one is the last one again.
    assert admin.delete(f"/api/users/{ADMIN_USER}").status_code == 400


def test_deleting_an_unknown_user_is_404(admin: TestClient):
    assert admin.delete("/api/users/no_such_person").status_code == 404


# ---------------------------------------------------------------- password change
def test_admin_can_change_another_users_password(
    app, admin: TestClient, poweruser: TestClient
):
    """Resetting a forgotten password is the admin's job; there is no email
    round-trip in this tool and no other way back in."""
    resp = admin.post(
        f"/api/users/{POWERUSER_USER}/password", json={"password": "brand-new-pw"}
    )
    assert resp.status_code == 200

    with TestClient(app) as fresh:
        assert fresh.post(
            "/api/login",
            json={"username": POWERUSER_USER, "password": POWERUSER_PASSWORD},
        ).status_code == 401
        assert fresh.post(
            "/api/login",
            json={"username": POWERUSER_USER, "password": "brand-new-pw"},
        ).status_code == 200


def test_a_user_can_change_their_own_password(app, poweruser: TestClient):
    resp = poweruser.post(
        f"/api/users/{POWERUSER_USER}/password", json={"password": "self-chosen-pw"}
    )
    assert resp.status_code == 200

    with TestClient(app) as fresh:
        assert fresh.post(
            "/api/login",
            json={"username": POWERUSER_USER, "password": "self-chosen-pw"},
        ).status_code == 200


def test_a_poweruser_cannot_change_someone_elses_password(
    app, poweruser: TestClient
):
    """"Or self" is the whole exception. Without the username check, any
    poweruser could take over the admin account."""
    resp = poweruser.post(
        f"/api/users/{ADMIN_USER}/password", json={"password": "hijacked"}
    )
    assert resp.status_code == 403

    with TestClient(app) as fresh:
        assert fresh.post(
            "/api/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD}
        ).status_code == 200


# ------------------------------------------------------------- the role rename
# SPEC section 2 named the second role 'annotator'. That is amended (the SPEC
# is frozen, so the record lives here and in the code's docstrings): the role
# is 'poweruser' — still exactly two roles, admin untouched. The old name must
# be refused loudly, not mapped silently: a stale script that still says
# 'annotator' should fail at the call site with the fix in the message, never
# mint an account whose role no gate recognises. And because production stores
# hold role='annotator' rows written by older builds, the boot path must flip
# them — idempotently, since the boot block runs on every start.

# The users table exactly as older builds created it, DEFAULT 'annotator'
# included. Frozen here as text, NOT imported from auth.py: the boot migration
# under test only exists because stores with this shape are on disk, so the
# test must keep producing that shape after auth.py stops being able to.
OLD_USERS_DDL = """
    CREATE TABLE users (
        username      TEXT PRIMARY KEY,
        password_hash TEXT NOT NULL,
        role          TEXT NOT NULL DEFAULT 'annotator',
        created_at    TIMESTAMP NOT NULL DEFAULT now()
    );
"""


def test_me_reports_the_poweruser_role(poweruser: TestClient):
    """The SPA renders its chrome off this one field, so the wire must say the
    new role name — a UI checking for 'poweruser' against an API still saying
    'annotator' would silently downgrade everyone to read-only."""
    resp = poweruser.get("/api/me")
    assert resp.status_code == 200
    assert resp.json() == {"username": POWERUSER_USER, "role": "poweruser", "age_enabled": True}


def test_create_user_accepts_poweruser(admin: TestClient):
    resp = admin.post(
        "/api/users",
        json={"username": "fresh_pu", "password": "pw", "role": "poweruser"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"username": "fresh_pu", "role": "poweruser"}
    listed = {u["username"]: u["role"] for u in admin.get("/api/users").json()}
    assert listed["fresh_pu"] == "poweruser"


def test_create_user_rejects_the_retired_annotator_role(admin: TestClient):
    """4xx with the fix in the message. Accepting the old name would recreate
    the exact rows the boot migration exists to clean up; rejecting it with a
    bare "bad request" would leave the caller guessing which roles exist."""
    resp = admin.post(
        "/api/users",
        json={"username": "old_script", "password": "pw", "role": "annotator"},
    )
    assert 400 <= resp.status_code < 500, resp.text
    assert "poweruser" in resp.text, (
        "the refusal must name the role to use instead"
    )
    names = {u["username"] for u in admin.get("/api/users").json()}
    assert "old_script" not in names, "the rejected account must not half-exist"


def test_boot_flips_stored_annotator_rows_to_poweruser(store):
    """A store written by an older build — role='annotator' rows in the users
    table — comes up with those rows flipped to 'poweruser', through the real
    boot path (`create_app`), and the flipped account both signs in and holds
    the new upload right. The password hash must ride through untouched: the
    scrypt format is self-describing and stable across builds, so a login
    failure here means the migration rewrote more than the role column.

    The DB-level anatomy of the flip (hash byte-identity, idempotent replay)
    is pinned in test_db.py next to the other old-shape migrations; this is
    the end-to-end proof through the HTTP surface."""
    from bienenblech import auth

    db_path = Path(store.paths.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        con.execute(OLD_USERS_DDL)
        con.execute(
            "INSERT INTO users VALUES (?, ?, 'annotator', now())",
            ["legacy_user", auth.hash_password("legacy-pw")],
        )
    finally:
        con.close()

    with TestClient(api.create_app(store)) as c:
        resp = c.post(
            "/api/login", json={"username": "legacy_user", "password": "legacy-pw"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"username": "legacy_user", "role": "poweruser", "age_enabled": True}
        assert c.get("/api/me").json()["role"] == "poweruser"

        up = c.post(
            "/api/images",
            files={"file": ("frame.png", frame_bytes(640, 480, seed=11), "image/png")},
        )
        assert up.status_code == 200, (
            "a migrated poweruser must hold the upload right: " + up.text
        )


# ------------------------------------------------------------ login Discord ping
# Contract: a SUCCESSFUL login posts one presence line to the Discord webhook,
# read from BIENENBLECH_DISCORD_WEBHOOK at the point of use (never Config/YAML),
# on a background thread that can never block or fail the login. The seam is
# api.py's own miniature of backup.py's: `api._notify_login(username)` spawns the
# thread, `api._login_poster` is the injectable poster, `api._redact` scrubs the
# URL from anything printed.

# Shaped like a real Discord webhook so api._WEBHOOK_ANY_RE and the redaction
# path are genuinely exercised, but the id and token cannot exist. Nothing ever
# opens a socket to it: the poster is always injected.
FAKE_WEBHOOK = (
    "https://discord.com/api/webhooks/000000000000000000/"
    "TEST-TOKEN-THIS-IS-NOT-A-REAL-WEBHOOK-0123456789"
)
FAKE_TOKEN = FAKE_WEBHOOK.rsplit("/", 1)[-1]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test in this module, ever, reaches a real webhook.

    The URL is read from the environment at the point of use, so a developer
    with `BIENENBLECH_DISCORD_WEBHOOK` exported in their shell would otherwise
    have every successful test login post into a real channel. Mirrors
    `test_backup.py`: the env var is unset, and both the module's default poster
    and the underlying `urlopen` are replaced so a test that forgets to inject
    a poster fails loudly instead of quietly posting over the wire."""
    monkeypatch.delenv(api.WEBHOOK_ENV, raising=False)

    def _refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "a test tried to reach the network / the real Discord poster"
        )

    monkeypatch.setattr(api, "_login_poster", _refuse)
    monkeypatch.setattr(urllib.request, "urlopen", _refuse)


class _SyncThreads:
    """Stands in for `threading` in api.py's namespace: `.start()` runs the
    target inline and records the spawn.

    Inline rather than joined-with-a-timeout because the assertion "the poster
    was called exactly once" is only meaningful once the thread has finished,
    and a sleep-based wait is exactly the flake these tests must not have. The
    spawn record still proves the post went through a daemon thread — the
    property that keeps a dead Discord from holding a login response hostage."""

    def __init__(self) -> None:
        self.spawned: list[Any] = []
        shim = self

        class Thread:
            def __init__(self, *, target: Callable[..., None], args: tuple = (),
                         name: str | None = None, daemon: bool | None = None) -> None:
                self.target, self.args = target, args
                self.name, self.daemon = name, daemon

            def start(self) -> None:
                shim.spawned.append(self)
                self.target(*self.args)

        self.Thread = Thread


class LoginRecorder:
    """An `api.LoginPoster` that records instead of posting, and optionally
    fails — the login-side sibling of test_backup.py's `Recorder`."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, str]] = []
        self.error = error

    def __call__(self, webhook: str, content: str) -> None:
        self.calls.append({"webhook": webhook, "content": content})
        if self.error is not None:
            raise self.error


@pytest.fixture()
def sync_threads(monkeypatch: pytest.MonkeyPatch) -> _SyncThreads:
    shim = _SyncThreads()
    monkeypatch.setattr(api, "threading", shim)
    return shim


@pytest.fixture()
def login_posts(monkeypatch: pytest.MonkeyPatch) -> LoginRecorder:
    """A recording poster injected over the `_no_network` refusal."""
    recorder = LoginRecorder()
    monkeypatch.setattr(api, "_login_poster", recorder)
    return recorder


def _login(client: TestClient, username: str = ADMIN_USER,
           password: str = ADMIN_PASSWORD):
    return client.post("/api/login", json={"username": username, "password": password})


def test_successful_login_posts_one_ping_with_the_username(
    client: TestClient, monkeypatch, sync_threads, login_posts
):
    """One successful login, one post: the configured webhook, the username, and
    nothing that smells like a credential. The message is a presence line for
    the channel, not an audit record."""
    monkeypatch.setenv(api.WEBHOOK_ENV, FAKE_WEBHOOK)

    resp = _login(client)
    assert resp.status_code == 200

    assert len(login_posts.calls) == 1
    call = login_posts.calls[0]
    assert call["webhook"] == FAKE_WEBHOOK
    assert ADMIN_USER in call["content"]
    assert ADMIN_PASSWORD not in call["content"], "the ping carries password material"
    assert "scrypt" not in call["content"]

    # Through a daemon thread: the request must never wait on Discord, and a
    # hung post must never keep the process alive at shutdown.
    [thread] = sync_threads.spawned
    assert thread.daemon is True


def test_failed_login_posts_nothing(
    client: TestClient, monkeypatch, sync_threads, login_posts
):
    """Failures are for the server log, not the channel: a bad-password storm
    relayed to Discord would be an amplification annoyance, and 'someone is
    guessing passwords' is not a presence signal. Both failure shapes — wrong
    password and unknown user — must stay silent."""
    monkeypatch.setenv(api.WEBHOOK_ENV, FAKE_WEBHOOK)

    assert _login(client, password="wrong").status_code == 401
    assert _login(client, username="nobody_here", password="x").status_code == 401

    assert login_posts.calls == []
    assert sync_threads.spawned == [], "a failed login spawned a notification thread"


def test_unset_or_blank_webhook_posts_nothing_and_logins_still_succeed(
    client: TestClient, monkeypatch, sync_threads, login_posts
):
    """An unconfigured webhook is a fully supported state (SPEC section 8's rule,
    extended to logins), not an error and not a warning: no post, no thread, no
    noise, and the login proceeds exactly as before."""
    assert api.WEBHOOK_ENV not in os.environ  # _no_network guarantees this
    assert _login(client).status_code == 200

    monkeypatch.setenv(api.WEBHOOK_ENV, "")
    assert _login(client).status_code == 200

    monkeypatch.setenv(api.WEBHOOK_ENV, "   ")
    assert _login(client).status_code == 200

    assert login_posts.calls == []
    assert sync_threads.spawned == []


def test_webhook_is_read_from_the_env_at_the_point_of_use(
    client: TestClient, monkeypatch, sync_threads, login_posts
):
    """Point of use means per login, not at import or `create_app`: a webhook
    exported after the app booted takes effect on the very next login. That is
    the property that keeps the URL out of Config and YAML — `config/` is
    committed and bind-mounted read-only, so the environment is the only place
    a bearer credential may live."""
    assert _login(client).status_code == 200  # env unset at boot and first login
    assert login_posts.calls == []

    monkeypatch.setenv(api.WEBHOOK_ENV, FAKE_WEBHOOK)  # same app, no restart
    assert _login(client).status_code == 200
    assert [c["webhook"] for c in login_posts.calls] == [FAKE_WEBHOOK]


def test_a_raising_poster_still_yields_200_and_prints_no_webhook(
    client: TestClient, monkeypatch, sync_threads, capsys
):
    """The post must NEVER fail the login — an outage of a chat channel that
    locks every user out of the tool would be an absurd coupling. And the
    failure line it prints passes through `api._redact`: urllib embeds the full
    request URL in its exception attributes, so the raw exception text is
    exactly where the bearer credential would otherwise leak into the log."""
    monkeypatch.setenv(api.WEBHOOK_ENV, FAKE_WEBHOOK)
    exploding = LoginRecorder(
        error=RuntimeError(f"HTTP 401 Unauthorized for url {FAKE_WEBHOOK}")
    )
    monkeypatch.setattr(api, "_login_poster", exploding)

    resp = _login(client)
    assert resp.status_code == 200
    assert resp.json() == {"username": ADMIN_USER, "role": "admin", "age_enabled": True}
    assert exploding.calls, "the poster should have been reached"
    assert client.get("/api/me").status_code == 200, "the session must survive"

    out = capsys.readouterr().out
    assert FAKE_WEBHOOK not in out, "the raw webhook URL was printed"
    assert FAKE_TOKEN not in out, "the webhook token was printed"
    assert "<discord-webhook>" in out, "the failure must still be visible, redacted"


def test_a_failed_thread_spawn_never_fails_the_login(
    client: TestClient, monkeypatch, capsys
):
    """The other place the background post can die: `Thread.start()` itself
    ("can't start new thread" under memory pressure). Same rule — the login
    answers 200 and whatever is printed is redacted."""
    monkeypatch.setenv(api.WEBHOOK_ENV, FAKE_WEBHOOK)

    class ExplodingThreads:
        class Thread:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def start(self) -> None:
                raise RuntimeError(f"can't start new thread posting to {FAKE_WEBHOOK}")

    monkeypatch.setattr(api, "threading", ExplodingThreads)

    resp = _login(client)
    assert resp.status_code == 200
    assert resp.json()["username"] == ADMIN_USER

    out = capsys.readouterr().out
    assert FAKE_WEBHOOK not in out and FAKE_TOKEN not in out


def test_disabled_age_tool_is_absent_end_to_end(tmp_path):
    """tools.age: false (the production setting for now) must hide the tool
    end to end: /api/me says so, and the age router is simply not mounted -
    a hidden tool answering 404 is indistinguishable from one that does not
    exist, which is the point. The age STORE still boots and backs up; the
    switch is visibility, not data."""
    from fastapi.testclient import TestClient as TC
    from bienenblech.api import create_app
    from bienenblech.config import BackupCfg, Config, PathsCfg, ToolsCfg

    root = tmp_path / "store"
    cfg = Config(
        paths=PathsCfg(db_path=str(root / "main.duckdb"),
                       age_db_path=str(root / "age.duckdb"),
                       images_dir=str(root / "images"),
                       cache_dir=str(root / "cache"),
                       backups_dir=str(root / "backups")),
        backup=BackupCfg(enabled=False),
        tools=ToolsCfg(age=False),
    )
    import os
    os.environ.pop("BIENENBLECH_DISCORD_WEBHOOK", None)
    os.environ["BIENENBLECH_ADMIN_USER"] = "flag_admin"
    os.environ["BIENENBLECH_ADMIN_PASSWORD"] = "flag-admin-pw"
    try:
        app = create_app(cfg)
        with TC(app) as c:
            r = c.post("/api/login", json={"username": "flag_admin", "password": "flag-admin-pw"})
            assert r.status_code == 200 and r.json()["age_enabled"] is False
            assert c.get("/api/me").json()["age_enabled"] is False
            assert c.get("/api/age/stats").status_code == 404
            assert c.get("/api/age/samples").status_code == 404
            # the age store exists regardless - visibility, not data
            assert (root / "age.duckdb").exists()
    finally:
        os.environ.pop("BIENENBLECH_ADMIN_USER", None)
        os.environ.pop("BIENENBLECH_ADMIN_PASSWORD", None)
