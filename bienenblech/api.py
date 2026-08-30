"""FastAPI surface: a thin JSON + JPEG layer over the DuckDB store, and the one
place where crop-local and source-image coordinates meet.

The routes here are deliberately dumb. Anything with a rule in it lives next
door - tiling and the coordinate transform in `crops.py`, dedupe and the
derivative in `uploads.py`, the schema in `db.py` - because the thing this file
must get right is not logic, it is *shape*: SPEC section 5 fixes the paths and
section 6 fixes the JSON, and a frontend written against those types is being
compiled right now.

Four things worth knowing before editing:

*   **Points on the wire are CROP-LOCAL, points in the DB are SOURCE-IMAGE.**
    Every conversion goes through `crops.to_source` / `crops.to_crop_local`;
    there is no offset arithmetic anywhere in this file. A polygon that reloads
    shifted is the most likely bug in this codebase and it is only cheap to find
    while there is exactly one pair of functions to suspect.
*   **Errors are mapped by exception handlers, not by try/except in routes.**
    `db.NotFound` -> 404, `db.DuplicateClass` -> 409, `db.DbBusy` -> 503,
    `uploads.UploadTooLarge` -> 413, any other `ValueError` -> 400 (which
    `export.EmptyExport` is, deliberately). Route bodies therefore read as the
    happy path, which is the point. db.py's `get_*` helpers return `None` for an
    unknown id rather than raising, so `_need()` is what turns that into the 404.
*   **Nothing that leaves this file carries a filesystem path.** db row dicts
    include server-side extras (`stored_path`, `sha256`, `bytes`, `image_id`);
    every browser-facing payload is shaped field by field so those never reach a
    JSON response, where they would be free reconnaissance.
*   **`/api/crops/next` is declared before `/api/crops/{crop_id}`.** Starlette
    matches in declaration order, so the literal must come first or "next" is
    read as a crop id and every queue request 404s.

The Age tool's routes (/api/age/*) live in `age.py` and are included as a
router by `create_app`; they inherit the app-level login gate and the exception
handlers above — but they run on their OWN store: modular per-tool storage
(owner decision) puts `age_samples` in `paths.age_db_path` with its own
backup_runs/meta, while users deliberately stay global in the main store so
auth and sessions are untouched. The API surface itself is unchanged by the
split. Only `/api/picker/examples` stays here, because it is the one endpoint
that needs both tools — it now queries each store on its own connection
(`get_con` + `get_age_con`) and merges.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, auth, crops, db, uploads
from .config import Config, load_config

# Reachable without a session. `/api/me` is in here because it is the SPA's
# "am I logged in?" probe - it answers 401 itself rather than being gated, so
# the login screen can render instead of the browser inventing an error.
PUBLIC_API = frozenset({"/api/health", "/api/login", "/api/logout", "/api/me"})

# The crop JPEG at a given path is a pure function of an immutable derivative and
# an immutable rect, so it can be cached hard. `private` is load-bearing: the
# pixels are session-gated and must never be stored by Caddy or a shared proxy.
IMMUTABLE_CACHE = "private, max-age=31536000, immutable"


# --------------------------------------------------------------- request bodies
class LoginReq(BaseModel):
    username: str
    password: str


class CreateUserReq(BaseModel):
    username: str
    password: str
    role: str = "poweruser"


class PasswordReq(BaseModel):
    password: str


class CreateClassReq(BaseModel):
    name: str
    color: str | None = None
    description: str | None = None


class UpdateClassReq(BaseModel):
    name: str | None = None
    color: str | None = None
    description: str | None = None


class CompleteReq(BaseModel):
    is_empty: bool = False


class CreateMaskReq(BaseModel):
    crop_id: str
    class_id: str
    # Deliberately untyped: `list[list[float]]` would let pydantic answer 422 for
    # a malformed polygon, and SPEC section 5 says a bad polygon is a 400 with a
    # sentence the user can act on. crops.validate_points writes that
    # sentence.
    points: Any = None


class UpdateMaskReq(BaseModel):
    class_id: str | None = None
    points: Any = None


class BackupRunReq(BaseModel):
    force: bool = False


# ---------------------------------------------------------------------- session
def _session_secret() -> str:
    """The signing key for the session cookie.

    `BIENENBLECH_SECRET` or nothing: an ephemeral key means every restart logs
    every user out mid-crop, so in a container that is a boot failure, not
    a warning. On a dev box it is merely annoying, so there it is a warning and
    a generated key.
    """
    secret = os.environ.get("BIENENBLECH_SECRET", "").strip()
    if secret:
        return secret
    in_container = Path("/.dockerenv").exists() or os.environ.get("BIENENBLECH_IN_CONTAINER")
    if in_container:
        raise RuntimeError(
            "BIENENBLECH_SECRET is not set. Refusing to boot with an ephemeral "
            "session key: every restart would sign out every user. Set it in "
            "the compose environment."
        )
    print(
        "[bienenblech.auth] BIENENBLECH_SECRET is unset - using a throwaway session "
        "key. Sessions will not survive a restart. Set it before deploying.",
        flush=True,
    )
    return secrets.token_hex(32)


def require_login(request: Request) -> None:
    """App-wide gate: every /api/* call needs a session bar the public handshake.

    Mounted as an app-level dependency rather than per route so a route added
    later is protected by default - the failure mode of an opt-in gate is a
    silently public endpoint.
    """
    path = request.url.path
    if not path.startswith("/api/") or path in PUBLIC_API:
        return
    if not request.session.get("user"):
        raise HTTPException(401, "login required")


def current_user(request: Request) -> dict:
    """The session identity, for stamping writes. 401 when absent."""
    user = request.session.get("user") or {}
    if not user.get("username"):
        raise HTTPException(401, "login required")
    return {"username": user["username"], "role": user.get("role")}


def require_admin(user: dict = Depends(current_user)) -> dict:
    if not auth.is_admin(user.get("role")):
        raise HTTPException(403, "admin only")
    return user


# ------------------------------------------------------------------ login alert
# The Discord channel doubles as a presence feed: a short "who just signed in"
# line tells the team the box is alive and being used. SPEC section 8's
# credential discipline extends here unchanged - the webhook URL is a bearer
# credential for posting into the channel, so it comes from the environment at
# the point of use, never from Config or YAML (`config/` is committed), and it
# must never reach a log line, which is why the one failure print below passes
# through `_redact` first.
#
# Deliberately a local miniature of backup.py's webhook handling rather than an
# import of it. backup.py's import discipline is the mirror image (a CLI rescue
# of the labels must not need this module or Pillow importable), and a login
# route that pulls in the archival machinery to send one HTTP POST is the wrong
# coupling in the other direction - each module owns its own tiny copy on
# purpose.

WEBHOOK_ENV = "BIENENBLECH_DISCORD_WEBHOOK"

_LOGIN_ALERT = "[bienenblech.alert] LOGIN"

# Redaction net for text that merely CONTAINS a webhook URL: urllib embeds the
# full request URL in HTTPError attributes, so an exception string is exactly
# where the credential would otherwise leak.
_WEBHOOK_ANY_RE = re.compile(
    r"https?://\S*discord(?:app)?\.com/api/webhooks/\S+", re.IGNORECASE
)

# poster(webhook_url, content) -> None, raising on failure. Bound at module
# level and looked up at call time, same seam as backup.py's injected Poster:
# a test monkeypatches `api._login_poster` and sees exactly what would have
# been posted, with no network anywhere.
LoginPoster = Callable[[str, str], None]


def _redact(text: str, webhook: str) -> str:
    """Scrub the webhook URL and its token from a string before it is printed.

    A webhook URL in a log line defeats the point of keeping it out of YAML:
    anyone who can read the log can post as this box. Same approach as
    backup.py's `_redact`, reimplemented small because this module must not
    import backup (see the section comment above)."""
    out = _WEBHOOK_ANY_RE.sub("<discord-webhook>", text or "")
    if webhook:
        out = out.replace(webhook, "<discord-webhook>")
        token = webhook.rstrip("/").rsplit("/", 1)[-1]
        # Only a real token: replacing a short trailing segment would chew
        # holes in unrelated text.
        if len(token) >= 8:
            out = out.replace(token, "<token>")
    return out


def _post_login_webhook(webhook: str, content: str) -> None:
    """Default poster: one stdlib JSON POST, nothing else.

    urllib rather than a dependency, matching backup.py's poster and for the
    same reasons (no `requests`, and no subprocess `curl` that would park the
    URL in the argv table). The 5 s timeout is the whole budget the daemon
    thread gets, so a black-holed webhook ties up one thread briefly, not
    forever."""
    body = json.dumps({"content": content[:1900]}).encode()
    req = urllib.request.Request(
        webhook, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "bienenblech-api/1.0"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        resp.read()


_login_poster: LoginPoster = _post_login_webhook


def _send_alert(webhook: str, content: str) -> None:
    """Thread body for any presence ping: post, swallow every failure.

    Rides `_login_poster` - the one injectable seam - so a test that patches
    the poster sees every ping the app would send, whatever the event."""
    try:
        _login_poster(webhook, content)
    except Exception as e:  # noqa: BLE001 - the webhook must never surface to a request
        print(f"{_LOGIN_ALERT} webhook failed: {_redact(str(e) or repr(e), webhook)}", flush=True)


def _notify(content: str, *, thread_name: str) -> None:
    """Fire-and-forget Discord ping. The shared discipline for every event:
    env var read at the point of use, unset is a supported no-op, the post
    rides a daemon thread, and nothing here may ever change a route's
    response."""
    webhook = os.environ.get(WEBHOOK_ENV, "").strip()
    if not webhook:
        return
    try:
        threading.Thread(
            target=_send_alert, args=(webhook, content),
            name=thread_name, daemon=True,
        ).start()
    except Exception as e:  # noqa: BLE001 - "can't start new thread" under load
        print(f"{_LOGIN_ALERT} webhook failed: {_redact(str(e) or repr(e), webhook)}", flush=True)


def _notify_login(username: str) -> None:
    """Ping for a SUCCESSFUL login.

    Successful only: failed attempts post nothing, because a bad-password storm
    posting to Discord is an amplification annoyance and the channel is for
    presence, not intrusion detection - the server log still carries the 401s."""
    _notify(f"bienenblech: '{username}' logged in",
            thread_name="bienenblech-login-alert")


def notify_queue_empty(tool: str, summary: str) -> None:
    """Ping when a tool's labeling queue just ran dry (owner request).

    Fires only on the TRANSITION to zero open items, which needs no debounce:
    the action that empties a queue cannot repeat while it stays empty, and a
    queue refilled (reopen, new upload) that later empties again has genuinely
    finished twice. Deletions that empty a queue do not ping - that is an
    admin's own act, not labeling news. `tool` names the database ('blech' or
    'age') because each tool is its own store."""
    _notify(f"bienenblech: {tool} queue is empty - {summary}",
            thread_name="bienenblech-queue-alert")


# ------------------------------------------------------------- response shaping
def _need(row: Mapping[str, Any] | None, what: str, ident: str) -> dict:
    """404 on a db lookup that came back empty.

    db.py's `get_image` / `get_crop` / `get_class` / `get_mask` answer `None` for
    an unknown id rather than raising, so that not-found stays a value until
    somebody has to turn it into a status code. This is that somebody.
    """
    if row is None:
        raise db.NotFound(f"unknown {what} {ident!r}")
    return dict(row)


def _mask_out(mask: Mapping[str, Any], crop: Mapping[str, Any]) -> dict:
    """A db mask row -> the SPEC section 6 `Mask`, points converted to crop-local.

    `image_id` is on the row and is dropped here: the TS type does not carry it
    and the crop already identifies the frame.
    """
    return {
        "mask_id": mask["mask_id"],
        "crop_id": mask["crop_id"],
        "class_id": mask["class_id"],
        "points": crops.to_crop_local(mask["points"], crop),
        "created_by": mask.get("created_by"),
        "created_at": mask.get("created_at"),
        "updated_at": mask.get("updated_at"),
    }


def _crop_summary(row: Mapping[str, Any], n_masks: int | None = None) -> dict:
    """A db crop row -> the SPEC section 6 `CropSummary`.

    Shaped field by field rather than passed through: the frontend's types are
    binding, a stray column from a future migration must not quietly become
    something a page depends on, and the row's `image_id` has no place in a
    CropSummary.
    """
    counted = row.get("n_masks")
    return {
        "crop_id": row["crop_id"],
        "row_idx": int(row["row_idx"]),
        "col_idx": int(row["col_idx"]),
        "x": int(row["x"]),
        "y": int(row["y"]),
        "w": int(row["w"]),
        "h": int(row["h"]),
        "status": row.get("status") or "open",
        "is_empty": bool(row.get("is_empty")),
        "n_masks": int(counted) if counted is not None else int(n_masks or 0),
        "completed_by": row.get("completed_by"),
        "completed_at": row.get("completed_at"),
    }


def _crop_task(con: Any, crop: Mapping[str, Any]) -> dict:
    """Build the `CropTask` the labeling screen runs on.

    `index` is 1-BASED. SPEC section 6 only says "position in this image's crop
    grid"; the editor workstream's progress component takes a 1-based prop and
    prints "Crop {index} of {total}" verbatim, so 0-based would greet every
    user with "Crop 0 of 24". The order is `row_idx` then `col_idx` -
    reading order - which is also the order `next_open_crop` walks, so "next"
    always moves forward in the progress line.

    `n_done` amends SPEC section 6's binding `CropTask`, and the amendment earns
    itself: without it the Label page has to fire a second request,
    `GET /api/images/{image_id}`, after every single completed crop purely to
    read one number back for its progress bar. The number was already in hand -
    `db.get_image` computes `n_done` in the very query this function runs anyway
    for the image block, so the round-trip bought nothing. It sits at the top
    level beside `index` and `total` because those three are one thought, the
    progress line; the `image` block stays a minimal identity/dimensions record.
    `n_crops` is deliberately NOT added: `total` is already that count, and two
    fields obliged to agree are two fields that will eventually disagree.
    """
    image = _need(db.get_image(con, crop["image_id"]), "image", crop["image_id"])
    siblings = db.list_crops(con, crop["image_id"])      # already in grid order
    index = next(
        (i for i, r in enumerate(siblings, start=1) if r["crop_id"] == crop["crop_id"]),
        0,
    )
    masks = [_mask_out(m, crop) for m in db.list_masks(con, crop_id=crop["crop_id"])]
    return {
        "crop": _crop_summary(crop, n_masks=len(masks)),
        "image": {
            "image_id": image["image_id"],
            "filename": image.get("filename"),
            "width": int(image["width"]),
            "height": int(image["height"]),
        },
        "masks": masks,
        "index": index,
        "total": len(siblings),
        "n_done": int(image.get("n_done") or 0),
    }


def _file_etag(path: Path) -> tuple[str, dict[str, str]]:
    """Strong ETag + immutable caching headers for a rendered image."""
    st = path.stat()
    digest = hashlib.sha256(f"{path.name}:{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()
    etag = f'"{digest[:32]}"'
    return etag, {"ETag": etag, "Cache-Control": IMMUTABLE_CACHE}


def _form_bool(value: str | None, field: str = "is_empty") -> bool:
    """A boolean out of a multipart form field, where booleans arrive as
    strings (the frontend's FormData sends "true"/"false"). Absent means false.
    An unrecognisable value is a 400 via the ValueError handler, not a silent
    false - a garbled true must not quietly queue N crops of nothing."""
    if value is None:
        return False
    v = str(value).strip().lower()
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("", "false", "0", "no", "off"):
        return False
    raise ValueError(f"{field} must be true or false, got {value!r}")


# ------------------------------------------------------------------ app factory
def create_app(config: Config | None = None) -> FastAPI:
    """Build the application. Also the uvicorn factory target (see cli.serve)."""
    if config is None:
        # load_config already consults $BIENENBLECH_CONFIG and falls back to the
        # committed example, so a fresh clone serves with no setup step.
        config = load_config()

    # Imported inside the factory, not at module top: age.py imports this
    # module's auth deps (current_user / require_admin), so a top-level import
    # in BOTH directions would be a genuine cycle. This direction runs only
    # after api.py is fully loaded.
    from . import age

    for directory in (config.paths.images_dir, config.paths.cache_dir,
                      config.paths.backups_dir, age.age_dir(config)):
        Path(directory).mkdir(parents=True, exist_ok=True)

    # Boot block. Idempotent by construction (CREATE IF NOT EXISTS, additive
    # migrations), so a fresh install and a five-year-old DB both boot straight
    # into a schema the routes can rely on. `init_db` already brings the users
    # table up; `ensure_user_table` is repeated because it costs nothing and
    # nothing here should depend on that ordering staying true.
    #
    # BOTH stores are opened and initialised here — modular per-tool storage
    # (owner decision): the main store (users + Blech + its own backup_runs/
    # meta) and the age store (age_samples + its own pair), each
    # self-describing and detachable. Users stay global in the main store on
    # purpose: one login, one role, everywhere. The one-time
    # `migrate_legacy_age_samples` runs while both connections are in hand —
    # it copies a pre-split main store's age_samples rows over (resume-safe)
    # and drops the legacy table, printing one line; a never-pre-split store
    # boots silently through it.
    boot = db.connect(config)
    try:
        db.init_db(boot)
        auth.ensure_user_table(boot)
        warning = auth.bootstrap_admin(boot)
        if warning:
            print(f"[bienenblech.auth] {warning}", flush=True)
        if Path(config.paths.db_path).resolve() == Path(config.paths.age_db_path).resolve():
            # Both tools pointed at one file defeats the split and would let
            # the legacy-table drop below destroy the live age data. Loud and
            # fatal on purpose: this is a config error, not a runtime state.
            raise ValueError(
                "paths.age_db_path must differ from paths.db_path - the age "
                "tool lives in its own store"
            )
        boot_age = db.connect_age(config)
        try:
            db.init_age_db(boot_age)
            db.migrate_legacy_age_samples(boot, boot_age)
        finally:
            boot_age.close()
    finally:
        boot.close()

    if getattr(config.backup, "enabled", False):
        # The export workstream owns backup.py and it may land after this file;
        # imported lazily, and wrapped, because a backup that cannot start is a
        # thing to shout about - never a reason the labeling tool will not boot.
        try:
            from . import backup

            backup.start_scheduler(config)
        except Exception as exc:    # noqa: BLE001 - boot must survive anything here
            print(f"[bienenblech.alert] BACKUP scheduler did not start: {exc}", flush=True)

    app = FastAPI(title="Bienenblech API", version=__version__,
                  dependencies=[Depends(require_login)])
    # No CORS: the SPA is served from this same origin in production, and the
    # Vite dev server proxies /api, so a wildcard origin would only ever be an
    # invitation. SessionMiddleware is the sole middleware, so it is also the
    # innermost - request.session is populated before require_login runs.
    app.add_middleware(
        SessionMiddleware,
        secret_key=_session_secret(),
        https_only=bool(config.auth.https_only),
        same_site="lax",
        max_age=config.auth.session_max_age,
    )

    def _error(status: int, exc: Exception, fallback: str) -> JSONResponse:
        return JSONResponse({"detail": str(exc) or fallback}, status_code=status)

    app.add_exception_handler(db.NotFound, lambda r, e: _error(404, e, "not found"))
    app.add_exception_handler(db.DuplicateClass, lambda r, e: _error(409, e, "duplicate"))
    app.add_exception_handler(db.DbBusy, lambda r, e: _error(503, e, "database is busy"))
    app.add_exception_handler(uploads.UploadTooLarge, lambda r, e: _error(413, e, "too large"))
    app.add_exception_handler(FileNotFoundError, lambda r, e: _error(404, e, "file not found"))
    # Last, so the more specific classes above win the MRO lookup.
    app.add_exception_handler(ValueError, lambda r, e: _error(400, e, "bad request"))

    def get_con():
        """A MAIN-STORE connection per request, closed when the response is done.

        Read-write for everyone: DuckDB refuses a second connection to the same
        file opened with a different mode in one process, and the backup thread
        holds a writer - a read_only reader opening in that window would fail
        the writer, not itself.
        """
        con = db.connect(config)
        try:
            yield con
        finally:
            con.close()

    def get_age_con():
        """An AGE-STORE connection per request — get_con's exact twin (same
        lifecycle, same read-write reasoning, the age backup thread holds a
        writer on ITS file too), opening `paths.age_db_path` instead. Only the
        picker endpoint below uses it in this module; the /api/age routes carry
        their own copy inside age.py's router, since these closures do not
        export."""
        con = db.connect_age(config)
        try:
            yield con
        finally:
            con.close()

    # ------------------------------------------------------------------ health
    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__, "schema_version": db.SCHEMA_VERSION}

    # -------------------------------------------------------------------- auth
    @app.post("/api/login")
    def login(body: LoginReq, request: Request, con: Any = Depends(get_con)):
        """Session handshake, plus a presence ping to the Discord channel.

        The ping follows SPEC section 8's credential discipline extended to
        login events: webhook URL from the environment at the point of use,
        never from Config or YAML, and redacted from anything printed. It fires
        on success only, off-thread, and nothing it does - unset, slow, down,
        raising - may change this route's response. See `_notify_login`.
        """
        user = auth.verify_user(con, body.username.strip(), body.password)
        if not user:
            # No Discord post for a failure, by design - see `_notify_login`.
            raise HTTPException(401, "invalid username or password")
        # Clear before writing: a session fixed by an attacker before login must
        # not survive into the authenticated session.
        request.session.clear()
        request.session["user"] = {"username": user["username"], "role": user["role"]}
        print(f"[bienenblech.alert] LOGIN user={user['username']}", flush=True)
        _notify_login(user["username"])
        return {"username": user["username"], "role": user["role"],
                "age_enabled": bool(config.tools.age)}

    @app.post("/api/logout")
    def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    @app.get("/api/me")
    def me(user: dict = Depends(current_user)):
        """The session identity plus the tool roster: `age_enabled` tells the
        SPA whether the Age tool exists at all on this deployment, so hiding a
        tool is one config line rather than a frontend build."""
        return {**user, "age_enabled": bool(config.tools.age)}

    # ------------------------------------------------------------------- users
    @app.get("/api/users", dependencies=[Depends(require_admin)])
    def list_users(con: Any = Depends(get_con)):
        return auth.list_users(con)

    @app.post("/api/users", dependencies=[Depends(require_admin)])
    def create_user(body: CreateUserReq, con: Any = Depends(get_con)):
        auth.create_user(con, body.username.strip(), body.password, body.role)
        return {"username": body.username.strip(), "role": body.role}

    @app.delete("/api/users/{username}", dependencies=[Depends(require_admin)])
    def delete_user(username: str, con: Any = Depends(get_con)):
        """Delete an account, unless it is the last admin - an instance with no
        admin can never be administered again, and there is no shell in the
        container to fix it from."""
        target = next((u for u in auth.list_users(con) if u["username"] == username), None)
        if target is None:
            raise HTTPException(404, f"unknown user {username}")
        if auth.is_admin(target.get("role")) and auth.count_admins(con) <= 1:
            raise HTTPException(400, "cannot delete the last admin")
        auth.delete_user(con, username)
        return {"ok": True}

    @app.post("/api/users/{username}/password")
    def set_user_password(username: str, body: PasswordReq,
                          user: dict = Depends(current_user),
                          con: Any = Depends(get_con)):
        """Admins may set anyone's password; everyone may set their own."""
        if user["username"] != username and not auth.is_admin(user.get("role")):
            raise HTTPException(403, "admin only")
        auth.set_password(con, username, body.password)
        return {"ok": True}

    # ------------------------------------------------------------------ images
    @app.post("/api/images")
    def upload_images(file: list[UploadFile] = File(...),
                      is_empty: str | None = Form(None),
                      user: dict = Depends(current_user),
                      con: Any = Depends(get_con)):
        """Land one or more frames and tile each of them.

        `is_empty` (optional form field, default false) applies to every file
        in the request - the frontend sends one file per request for honest
        progress bars, so that is per-file control in practice. WHY it exists:
        the second photo of every sheet is the cleaned, empty one, and making
        the uploader assert emptiness once beats making someone click through
        N crops of nothing. The crops of an empty-marked frame are born done +
        empty, attributed to the uploader, so the export gains real negative
        samples (0-byte label files) with zero labeling work. Dedupe rule: the
        flag never touches an existing frame - a sha256 match still answers
        "duplicate, nothing was changed", and that stays true whatever
        `is_empty` says.

        Open to any signed-in user, not just admins — an amendment to SPEC
        section 2, recorded here because the SPEC is frozen (section 11).
        Powerusers upload the sheets they label, and with exactly two roles,
        admin and poweruser, "signed in" is the honest spelling of "admin or
        poweruser". Deletion is a different animal — it destroys labeling
        hours — so DELETE /api/images/{image_id} stays admin-only.

        A sync def, so Starlette runs it in a threadpool: reading a 200 MB
        multipart part and re-encoding it with Pillow must not block the event
        loop while other users are labeling.

        Cheap checks (extension, size) run over the whole batch first, so a
        rejected batch is rejected before anything is written. A file that only
        fails on decode still leaves its predecessors stored - harmless, they are
        valid frames and the sha dedupe makes a retry of the batch idempotent.
        """
        if not file:
            raise HTTPException(400, "no files uploaded")
        empty = _form_bool(is_empty)    # parsed before any write, like the checks below
        limit = int(config.upload.max_mb) * 1024 * 1024
        allowed = {str(e).lower() for e in config.upload.allowed}
        for up in file:
            name = uploads.sanitise_filename(up.filename or "")
            if Path(name).suffix.lower() not in allowed:
                raise ValueError(
                    f"unsupported file type {name!r}; allowed: {', '.join(sorted(allowed))}"
                )
            if up.size is not None and up.size > limit:
                raise uploads.UploadTooLarge(
                    f"{name} is {up.size / 1e6:.1f} MB; the limit is {config.upload.max_mb} MB"
                )

        stored, duplicates = [], []
        for up in file:
            row, is_duplicate = uploads.store_upload(
                config, con,
                filename=up.filename or "upload.jpg",
                data=up.file.read(),
                username=user["username"],
                is_empty=empty,
            )
            (duplicates if is_duplicate else stored).append(uploads.image_summary(row))
        return {"images": stored, "duplicates": duplicates}

    @app.get("/api/images")
    def list_images(con: Any = Depends(get_con)):
        return [uploads.image_summary(r) for r in db.list_images(con)]

    @app.get("/api/images/{image_id}")
    def get_image(image_id: str, con: Any = Depends(get_con)):
        image = _need(db.get_image(con, image_id), "image", image_id)
        return {"image": uploads.image_summary(image),
                "crops": [_crop_summary(r) for r in db.list_crops(con, image_id)]}

    @app.get("/api/images/{image_id}/file")
    def get_image_file(image_id: str, request: Request, con: Any = Depends(get_con)):
        image = _need(db.get_image(con, image_id), "image", image_id)
        path = crops.source_path(config, image)
        if not path.exists():
            raise HTTPException(404, "stored image missing")
        etag, headers = _file_etag(path)
        if etag in (request.headers.get("if-none-match") or ""):
            return Response(status_code=304, headers=headers)
        return FileResponse(str(path), media_type="image/jpeg", headers=headers)

    @app.delete("/api/images/{image_id}", dependencies=[Depends(require_admin)])
    def delete_image(image_id: str, force: bool = False, con: Any = Depends(get_con)):
        """The one hard delete in this system (SPEC section 4).

        It refuses an image that carries masks unless `?force=true`, because
        labeling hours are the only thing on this box that cannot be
        regenerated and a mis-clicked row in an image list is a plausible way to
        lose a week of them.
        """
        _need(db.get_image(con, image_id), "image", image_id)    # 404 before any write
        n_masks = len(db.list_masks(con, image_id=image_id))
        if n_masks and not force:
            raise HTTPException(
                409,
                f"image has {n_masks} mask(s); re-send with ?force=true to delete them too",
            )
        db.delete_image(con, image_id)
        uploads.remove_image_files(config, image_id)
        return {"ok": True, "deleted_masks": n_masks}

    # ------------------------------------------------------------------- crops
    # Declared before /api/crops/{crop_id}: Starlette matches in declaration
    # order and "next" would otherwise be read as a crop id.
    @app.get("/api/crops/next")
    def next_crop(image_id: str | None = None, con: Any = Depends(get_con)):
        """The queue: the oldest open crop, optionally within one image.

        204 rather than 404 when there is nothing left - an empty queue is a
        success (everything is labeled), and the SPA shows a different screen
        for it than for a broken request.
        """
        crop = db.next_open_crop(con, image_id=image_id)
        if not crop:
            return Response(status_code=204)
        return _crop_task(con, crop)

    @app.get("/api/crops/{crop_id}")
    def get_crop(crop_id: str, con: Any = Depends(get_con)):
        return _crop_task(con, _need(db.get_crop(con, crop_id), "crop", crop_id))

    @app.get("/api/crops/{crop_id}/image")
    def get_crop_image(crop_id: str, request: Request, con: Any = Depends(get_con)):
        """The crop's pixels, rendered on demand and cached on disk."""
        crop = _need(db.get_crop(con, crop_id), "crop", crop_id)
        image = _need(db.get_image(con, crop["image_id"]), "image", crop["image_id"])
        path = crops.render_crop(config, image, crop)
        etag, headers = _file_etag(path)
        if etag in (request.headers.get("if-none-match") or ""):
            return Response(status_code=304, headers=headers)
        return FileResponse(str(path), media_type="image/jpeg", headers=headers)

    @app.post("/api/crops/{crop_id}/complete")
    def complete_crop(crop_id: str, body: CompleteReq,
                      user: dict = Depends(current_user),
                      con: Any = Depends(get_con)):
        """Mark a crop done.

        The two guards below are the completeness invariant from SPEC section 1,
        enforced rather than merely documented. `db.set_crop_status` stores what
        it is told and does not veto, so this route is the ONLY place the
        invariant can hold: a `done` crop with no masks and `is_empty=false`
        reaches the export as an image full of unlabeled instances, which
        actively teaches the model to suppress true positives. It is the one
        failure this tool exists to prevent, so it is a 400 and not a tooltip.
        `empty` is the honest way to say "nothing here" and is a valuable
        negative sample.
        """
        _need(db.get_crop(con, crop_id), "crop", crop_id)    # 404 before any write
        n_masks = len(db.list_masks(con, crop_id=crop_id))
        if not body.is_empty and n_masks == 0:
            raise HTTPException(
                400,
                "this crop has no masks - label every instance in it, or mark the "
                "crop empty if there is genuinely nothing in it",
            )
        if body.is_empty and n_masks:
            raise HTTPException(
                400,
                f"this crop has {n_masks} mask(s), so it is not empty - delete them "
                "first if it really contains nothing",
            )
        crop = db.set_crop_status(con, crop_id, status="done",
                                  is_empty=bool(body.is_empty), actor=user["username"])
        # Completing needs an open crop, so reaching zero open crops HERE is
        # exactly the transition to an empty queue - no state to track.
        st = db.stats(con)
        if st["n_crops"] > 0 and st["n_done"] == st["n_crops"]:
            notify_queue_empty(
                "blech",
                f"all {st['n_crops']} crops done, {st['n_masks']} polygons",
            )
        return _crop_task(con, crop)

    @app.post("/api/crops/{crop_id}/reopen")
    def reopen_crop(crop_id: str, user: dict = Depends(current_user),
                    con: Any = Depends(get_con)):
        """Put a crop back in the queue. Clears `is_empty` too: reopening means
        the completion no longer stands, and a still-empty crop is one click away."""
        crop = db.set_crop_status(con, crop_id, status="open", is_empty=False,
                                  actor=user["username"])
        return _crop_task(con, crop)

    # ----------------------------------------------------------------- classes
    @app.get("/api/classes")
    def list_classes(include_archived: bool = False, con: Any = Depends(get_con)):
        return db.list_classes(con, include_archived=include_archived)

    @app.post("/api/classes")
    def create_class(body: CreateClassReq, user: dict = Depends(current_user),
                     con: Any = Depends(get_con)):
        """Create a class. Open to any signed-in user, deliberately.

        SPEC section 2 grants the labeling role - now 'poweruser'; the frozen
        SPEC text says 'annotator' - "label crops, add classes, read" in so
        many words, so this route is not gated - and it is the one class route
        that is not. Creation is additive and local: a new class costs a fresh
        yolo_index (db.py's to assign, and never reused) and changes nothing
        anybody else has already labeled. Curation - renaming, recoloring,
        archiving, restoring - is not additive, so it is the admin's. See
        `update_class` and `archive_class` for that half of the rule."""
        name = body.name.strip()
        if not name:
            raise ValueError("class name must not be empty")
        return db.create_class(con, name=name, color=body.color,
                               description=body.description, actor=user["username"])

    @app.patch("/api/classes/{class_id}", dependencies=[Depends(require_admin)])
    def update_class(class_id: str, body: UpdateClassReq,
                     user: dict = Depends(current_user), con: Any = Depends(get_con)):
        """Rename, recolor or re-describe a class. ADMIN-ONLY.

        A rename or a recolor is exactly as globally visible as an archive: it
        rewrites every picker, every legend and every export's `data.yaml` for
        every user at once, and the poweruser who did it cannot see who else was
        mid-crop against the old name. So it sits on the same side of the line as
        archive and restore - creation is the poweruser's (SPEC section 2 grants
        "add classes"), curation is the admin's (section 2 grants admins
        "everything: users, classes, upload, delete, export, backup").

        SPEC section 5's endpoint table does not mark this route (admin). That
        table is resolved here against section 2 and amendment A4, which already
        put restore behind admin; the SPEC itself is not edited (section 11).
        `user` is still injected, for the `actor` stamp in class_audit.
        """
        fields = body.model_dump(exclude_unset=True)
        if "name" in fields and fields["name"] is not None:
            fields["name"] = fields["name"].strip()
            if not fields["name"]:
                raise ValueError("class name must not be empty")
        if not fields:
            return _need(db.get_class(con, class_id), "class", class_id)
        return db.update_class(con, class_id, actor=user["username"], **fields)

    @app.delete("/api/classes/{class_id}", dependencies=[Depends(require_admin)])
    def archive_class(class_id: str, user: dict = Depends(current_user),
                      con: Any = Depends(get_con)):
        """Archive, never drop: the class keeps its yolo_index forever so a model
        trained on an older export still matches today's data.yaml. ADMIN-ONLY.

        Archive and restore must be a MATCHED PAIR, and A4 already put restore
        behind admin. A role that can hide a class but not unhide it can only
        create a mess somebody else has to clean up: archiving takes the class
        out of every picker and refuses new masks on it (see `create_mask`),
        while its existing masks stay in the store as invisible work. So the two
        halves of that switch are gated together, and for the same reason
        `update_class` is gated too.

        SPEC section 5's endpoint table does not mark this route (admin); this
        resolves that against section 2 and A4, in code, without editing the SPEC
        (section 11). `user` is still injected, for the class_audit `actor`.
        """
        return db.archive_class(con, class_id, actor=user["username"])

    @app.post("/api/classes/{class_id}/restore", dependencies=[Depends(require_admin)])
    def restore_class(class_id: str, user: dict = Depends(current_user),
                      con: Any = Depends(get_con)):
        """Un-archive a class. Admin-only, and one line, but without it a
        mis-clicked archive is unfixable from the UI - and the class's masks are
        still there, waiting to become invisible work."""
        _need(db.get_class(con, class_id), "class", class_id)
        return db.update_class(con, class_id, archived=False, actor=user["username"])

    # ------------------------------------------------------------------- masks
    @app.post("/api/masks")
    def create_mask(body: CreateMaskReq, user: dict = Depends(current_user),
                    con: Any = Depends(get_con)):
        crop = _need(db.get_crop(con, body.crop_id), "crop", body.crop_id)
        klass = _need(db.get_class(con, body.class_id), "class", body.class_id)
        if klass.get("archived"):
            raise ValueError(
                f"class {klass.get('name', body.class_id)!r} is archived and cannot "
                "take new masks"
            )
        points = crops.validate_points(body.points, crop)
        mask = db.create_mask(
            con,
            crop_id=crop["crop_id"],
            image_id=crop["image_id"],
            class_id=body.class_id,
            points=crops.to_source(points, crop),
            actor=user["username"],
        )
        return _mask_out(mask, crop)

    @app.patch("/api/masks/{mask_id}")
    def update_mask(mask_id: str, body: UpdateMaskReq, con: Any = Depends(get_con)):
        existing = _need(db.get_mask(con, mask_id), "mask", mask_id)
        crop = _need(db.get_crop(con, existing["crop_id"]), "crop", existing["crop_id"])
        fields: dict[str, Any] = {}
        if body.class_id is not None:
            klass = _need(db.get_class(con, body.class_id), "class", body.class_id)
            if klass.get("archived"):
                raise ValueError(
                    f"class {klass.get('name', body.class_id)!r} is archived and "
                    "cannot take masks"
                )
            fields["class_id"] = body.class_id
        if body.points is not None:
            fields["points"] = crops.to_source(
                crops.validate_points(body.points, crop), crop
            )
        if not fields:
            return _mask_out(existing, crop)
        return _mask_out(db.update_mask(con, mask_id, **fields), crop)

    @app.delete("/api/masks/{mask_id}")
    def delete_mask(mask_id: str, user: dict = Depends(current_user),
                    con: Any = Depends(get_con)):
        """Soft delete (SPEC section 4). The row stays; the exporter skips it.

        Author-or-admin, by owner decision: deleting your own polygon and
        redrawing it is part of drawing, so it cannot be admin-only - but
        deleting someone ELSE's polygon destroys work you did not do, which
        makes it curation, and curation is the admin's. The other delete
        routes (users, frames, age samples) are already admin-gated; this is
        the rule that makes deletion coherent across the app."""
        mask = _need(db.get_mask(con, mask_id), "mask", mask_id)
        if user["role"] != "admin" and mask.get("created_by") != user["username"]:
            raise HTTPException(
                403,
                f"this polygon was drawn by {mask.get('created_by') or 'someone else'}; "
                "only its author or an admin can delete it",
            )
        db.soft_delete_mask(con, mask_id)
        return {"ok": True}

    # ------------------------------------------------------------------- stats
    @app.get("/api/stats")
    def stats(con: Any = Depends(get_con)):
        return db.stats(con)

    # ------------------------------------------------------------------ export
    @app.get("/api/export/yolo", dependencies=[Depends(require_admin)])
    def export_yolo(val_fraction: float = 0.2, seed: int = 0,
                    con: Any = Depends(get_con)):
        """Stream a YOLO-seg dataset zip.

        Built into a temp file rather than into memory (an export carries every
        done crop's JPEG) and unlinked by a BackgroundTask once the response has
        been sent - which is the only point at which the file is safe to remove.

        A store with no `done` crop raises `export.EmptyExport`, a ValueError, so
        it lands as a 400 whose detail says exactly that: an empty dataset is a
        request that cannot be honoured yet, not a server fault.
        """
        from . import export      # owned by the export workstream

        if not 0.0 <= val_fraction < 1.0:
            raise ValueError(f"val_fraction must be in [0, 1), got {val_fraction}")
        tmpdir = Path(tempfile.mkdtemp(prefix="bienenblech-export-"))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out = tmpdir / f"bienenblech-yolo-{stamp}.zip"
        try:
            export.build_yolo_zip(config, con, val_fraction=val_fraction, seed=seed,
                                  out_path=out)
        except BaseException:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise
        return FileResponse(
            str(out), media_type="application/zip", filename=out.name,
            background=BackgroundTask(shutil.rmtree, str(tmpdir), ignore_errors=True),
        )

    # ------------------------------------------------------------------ backup
    @app.get("/api/backup/status")
    def backup_status():
        """Scheduler state plus the recent runs, passed through verbatim.

        Each run carries `delivery` as well as `delivered`: "no webhook
        configured", "over the size cap, only a summary posted" and "the webhook
        refused it" are three different operator situations, and a boolean
        collapses them into one indistinguishable false.

        Never 500s. A status probe that errors during a routine write looks
        exactly like an outage, so a missing or unhappy backup module is reported
        as data in the same shape.

        Not admin-gated: SPEC section 5 marks only `/api/backup/run` admin. The
        run rows therefore show `zip_path` to any signed-in user - acceptable
        only because that directory is `paths.backups_dir` from the committed
        config and is not a secret. If backups ever move somewhere that is, gate
        this route rather than filtering the field.
        """
        try:
            from . import backup

            return backup.status(config)
        except Exception as exc:    # noqa: BLE001 - status must always answer
            return {"enabled": bool(getattr(config.backup, "enabled", False)),
                    "interval_days": getattr(config.backup, "interval_days", None),
                    "keep": getattr(config.backup, "keep", None),
                    "max_upload_mb": getattr(config.backup, "max_upload_mb", None),
                    "webhook_configured": False, "webhook_valid": False,
                    "scheduler": None, "due": None, "due_reason": None,
                    "watermark": None, "last_run": None, "next_due": None,
                    "runs": [], "error": str(exc)}

    @app.post("/api/backup/run", dependencies=[Depends(require_admin)])
    def backup_run(body: BackupRunReq | None = None):
        """Run a backup inside the process that already holds the DB - the
        contention-free path, so an operator without shell access never turns a
        transient lock into a disabled weekly job."""
        from . import backup

        return backup.run_backup(config, trigger="manual", force=bool(body and body.force))

    # ---------------------------------------------------------------- age tool
    # The whole /api/age surface lives in age.py (imported at the top of this
    # factory); it shares this app's require_login gate and exception handlers
    # but rides its OWN store via age.py's get_age_con (modular per-tool
    # storage). Included before the SPA catch-all below for the same
    # declaration-order reason as every /api route.
    if config.tools.age:
        app.include_router(age.create_router(config))

    # ------------------------------------------------------------------ picker
    @app.get("/api/picker/examples")
    def picker_examples(con: Any = Depends(get_con),
                        age_con: Any = Depends(get_age_con)):
        """One representative id per tool, so the tool picker's tiles can show
        real data: `{"blech": crop_id | null, "age": sample_id | null}`. Lives
        here, not in either tool's module, because it is the one endpoint that
        reads both tools' data — the picker sits above both tools exactly the
        way this file sits above their routers. Since the stores split it asks
        each store on its OWN connection and merges the two answers here — no
        cross-database ATTACH, ever. Null when a tool is empty; the picker
        renders its quiet fallback tile for that."""
        return {"blech": db.picker_example_blech(con),
                "age": db.picker_example_age(age_con)}

    # -------------------------------------------------------- static SPA (last)
    # Mounted last so every /api route above wins the match. Resolved from the
    # package's parent so it works from a checkout and from /app in the image.
    dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    if dist.exists():
        assets = dist / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            """Serve the built SPA, with an index.html fallback for client-side
            routes. `/api/*` never falls through to index.html: a 200 page where
            the SPA expected JSON turns a typo'd route into an unreadable parse
            error instead of a 404."""
            if full_path.startswith("api/"):
                raise HTTPException(404, "not found")
            root = dist.resolve()
            candidate = (dist / full_path).resolve()
            if full_path and root in candidate.parents and candidate.is_file():
                return FileResponse(str(candidate))
            return FileResponse(str(dist / "index.html"))
    else:
        @app.get("/{full_path:path}", include_in_schema=False)
        def spa_not_built(full_path: str):
            if full_path.startswith("api/"):
                raise HTTPException(404, "not found")
            raise HTTPException(
                404, "frontend/dist is not built - run `npm run build` in frontend/"
            )

    return app
