"""Bienenblech command line (Typer).

The operator's way in on a box with no browser session yet, and the recovery
path when there is no browser at all:

    python -m bienenblech.cli initdb
    python -m bienenblech.cli adduser alice --role poweruser
    python -m bienenblech.cli serve --host 0.0.0.0 --port 8000
    python -m bienenblech.cli export-yolo out.zip --val-fraction 0.2
    python -m bienenblech.cli backup --force

Every command opens its own short-lived DuckDB connection and closes it, exactly
like a request does, so running the CLI against a live server is a momentary
lock at worst rather than a second writer holding the store open. Imports of the
heavy modules (FastAPI, uvicorn, Pillow) live inside the commands so `--help`
stays instant and a broken optional dependency only breaks the command that
needs it.
"""
from __future__ import annotations

import os
from pathlib import Path

import typer

from .config import Config, load_config

app = typer.Typer(add_completion=False,
                  help="Polygon segmentation labeling on fixed-size crops.")

CONFIG_OPT = typer.Option(None, "--config", "-c",
                          help="Path to the YAML config (default: config/bienenblech.yaml).")


def _load(config: str | None) -> Config:
    return load_config(config)


@app.command()
def serve(
    config: str = CONFIG_OPT,
    host: str = typer.Option("127.0.0.1", help="Bind address. 0.0.0.0 in the container."),
    port: int = typer.Option(8000, help="Bind port."),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes (dev)."),
):
    """Run the API and serve the built SPA from frontend/dist."""
    import uvicorn

    if reload:
        # Reload needs an import string so the worker process can rebuild the app
        # after an edit; the config path therefore has to travel by environment
        # rather than by closure. create_app() reads the same variable.
        if config:
            os.environ["BIENENBLECH_CONFIG"] = config
        uvicorn.run("bienenblech.api:create_app", factory=True, host=host, port=port,
                    reload=True)
        return

    from .api import create_app

    uvicorn.run(create_app(_load(config)), host=host, port=port)


@app.command()
def initdb(config: str = CONFIG_OPT):
    """Create or upgrade the DuckDB schema. Idempotent."""
    from . import db

    cfg = _load(config)
    con = db.connect(cfg)
    try:
        db.init_db(con)
    finally:
        con.close()
    typer.echo(f"Initialized {cfg.paths.db_path} (schema v{db.SCHEMA_VERSION})")


@app.command()
def adduser(
    name: str = typer.Argument(..., help="Username."),
    config: str = CONFIG_OPT,
    role: str = typer.Option("poweruser", help="admin | poweruser."),
    password: str = typer.Option(None, help="Password (prompted if omitted)."),
):
    """Create a user account."""
    from . import auth, db

    cfg = _load(config)
    if not password:
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    con = db.connect(cfg)
    try:
        auth.ensure_user_table(con)
        auth.create_user(con, name, password, role)
    finally:
        con.close()
    typer.echo(f"Created {name} ({role})")


@app.command()
def passwd(
    name: str = typer.Argument(..., help="Username."),
    config: str = CONFIG_OPT,
    password: str = typer.Option(None, help="New password (prompted if omitted)."),
):
    """Set a user's password. The way back in after a forgotten admin login."""
    from . import auth, db

    cfg = _load(config)
    if not password:
        password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
    con = db.connect(cfg)
    try:
        auth.set_password(con, name, password)
    finally:
        con.close()
    typer.echo(f"Password updated for {name}")


@app.command()
def classes(config: str = CONFIG_OPT,
            include_archived: bool = typer.Option(
                False, "--include-archived",
                help="Also list archived classes (they keep their yolo_index).")):
    """List the label classes in yolo_index order.

    That is the order of an export's data.yaml, so this is how you check which
    index a model was trained on before pointing it at new data.
    """
    from . import db

    cfg = _load(config)
    con = db.connect(cfg)
    try:
        rows = db.list_classes(con, include_archived=include_archived)
    finally:
        con.close()
    if not rows:
        typer.echo("No classes yet. Add one from the Classes page or POST /api/classes.")
        return
    for c in rows:
        flag = " (archived)" if c["archived"] else ""
        typer.echo(f"{c['yolo_index']:>3}  {c['class_id']:<24} {c['color']}  "
                   f"{c['n_masks']:>6} masks  {c['name']}{flag}")


@app.command()
def stats(config: str = CONFIG_OPT):
    """Store-wide counters.

    The done/total ratio is the number to lead with: it is how much of this
    store is actually exportable, since an open crop never reaches a dataset.
    """
    from . import db

    cfg = _load(config)
    con = db.connect(cfg)
    try:
        s = db.stats(con)
    finally:
        con.close()
    pct = (100.0 * s["n_done"] / s["n_crops"]) if s["n_crops"] else 0.0
    typer.echo(f"images  {s['n_images']}")
    typer.echo(f"crops   {s['n_crops']}  ({s['n_done']} done, {pct:.1f}% exportable)")
    typer.echo(f"masks   {s['n_masks']}")
    for c in s["per_class"]:
        typer.echo(f"  {c['yolo_index']:>3}  {c['name']:<24} {c['n_masks']:>6}")


@app.command("export-yolo")
def export_yolo(
    out: Path = typer.Argument(..., help="Destination .zip path."),
    config: str = CONFIG_OPT,
    val_fraction: float = typer.Option(0.2, "--val-fraction",
                                       help="Share of images held out for val."),
    seed: int = typer.Option(0, "--seed", help="Split seed; the split is grouped by image."),
):
    """Write a YOLO11-seg dataset zip of every `done` crop."""
    from . import db, export

    cfg = _load(config)
    con = db.connect(cfg)
    try:
        summary = export.build_yolo_zip(cfg, con, val_fraction=val_fraction, seed=seed,
                                        out_path=out)
    except export.EmptyExport as exc:
        # A routine state, not a crash: a store where nobody has finished a crop
        # yet deserves the sentence, not a traceback.
        typer.echo(str(exc))
        raise typer.Exit(1)
    finally:
        con.close()
    typer.echo(f"Wrote {out}")
    typer.echo(f"  crops {summary.get('n_crops')} "
               f"(train {summary.get('n_train')} / val {summary.get('n_val')}), "
               f"masks {summary.get('n_masks')}, images {summary.get('n_images')}")


@app.command()
def backup(config: str = CONFIG_OPT,
           force: bool = typer.Option(False, "--force",
                                      help="Run even if the interval has not elapsed.")):
    """Run a backup now. Contention is reported as 'skipped', never as failure."""
    from . import backup as backup_mod

    result = backup_mod.run_backup(_load(config), trigger="cli", force=force)
    typer.echo(f"{result.get('status')}  {result.get('reason') or ''}".strip())
    if result.get("zip_path"):
        typer.echo(f"  {result['zip_path']}")
    if result.get("delivery"):
        # Not a boolean: "no webhook", "over the cap, summary only" and "refused"
        # are three different things for whoever is on the hook for the archive.
        typer.echo(f"  delivery: {result['delivery']}")
    if result.get("error"):
        typer.echo(f"  error: {result['error']}")
    if result.get("status") == "failed":
        raise typer.Exit(1)


@app.command("backup-status")
def backup_status(config: str = CONFIG_OPT):
    """Scheduler state plus the last few runs."""
    from . import backup as backup_mod

    s = backup_mod.status(_load(config))
    # The URL itself is never printed - `backup.status` does not return it, and a
    # webhook in a terminal scrollback is a webhook in a screenshot.
    if not s.get("webhook_configured"):
        webhook = "not set (still zips and rotates locally)"
    elif s.get("webhook_valid"):
        webhook = "configured"
    else:
        webhook = "configured, but does not look like a webhook URL"
    typer.echo(f"enabled   {s.get('enabled')}")
    typer.echo(f"webhook   {webhook}")
    typer.echo(f"due       {s.get('due')}  {s.get('due_reason') or ''}".rstrip())
    typer.echo(f"next_due  {s.get('next_due')}")
    if s.get("error"):
        typer.echo(f"error     {s['error']}")
    for run in s.get("runs") or []:
        typer.echo(f"  {run.get('started_at')}  {str(run.get('status')):<8} "
                   f"{str(run.get('trigger') or ''):<9} {run.get('delivery') or '-':<28} "
                   f"{run.get('zip_path') or ''}")
        if run.get("error"):
            typer.echo(f"      {run['error']}")


if __name__ == "__main__":
    app()
