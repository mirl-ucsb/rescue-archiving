"""Command-line interface (Typer) for the rescue-archiving pipeline.

Commands: init, doctor, add, list, show, verify, check, dedup, export.

The CLI is a thin shell: argument parsing + presentation. All real work lives
in capture / hashing / metadata / dedup / export, coordinated through a single
SQLite transaction per command so a failure rolls back cleanly.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from urllib.parse import urlparse

import typer

from . import capture, config, db, dedup, export, hashing

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Local-first counter-archival capture pipeline (human-in-the-loop).",
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _effective_cfg(operator: str | None = None, no_wayback: bool = False,
                   archivebox: bool | None = None) -> config.Config:
    cfg = config.get_config()
    changes: dict = {}
    if operator:
        changes["operator"] = operator
    if no_wayback:
        changes["wayback_enabled"] = False
    if archivebox is not None:
        changes["archivebox_enabled"] = archivebox
    return dataclasses.replace(cfg, **changes) if changes else cfg


def _classify_target(target: str) -> str:
    if not target or not target.strip():
        raise typer.BadParameter("target is empty: supply a URL or a file path")
    scheme = urlparse(target).scheme.lower()
    if scheme in ("http", "https"):
        return "url"
    if scheme in ("ftp", "ftps"):
        return "url"
    if Path(target).expanduser().is_file():
        return "file"
    raise typer.BadParameter(
        f"'{target}' is neither an http(s) URL nor an existing file path"
    )


def _echo(msg: str = "") -> None:
    typer.echo(msg)


def _bold(msg: str) -> str:
    return typer.style(msg, bold=True)


# ---------------------------------------------------------------------------
# init / doctor
# ---------------------------------------------------------------------------
@app.command()
def init() -> None:
    """Create the data tree and SQLite schema (idempotent)."""
    cfg = config.get_config()
    db.init_db(cfg)
    _echo(f"Initialised archive at {_bold(str(cfg.data_dir))}")
    _echo(f"  database : {cfg.db_path}")
    _echo(f"  originals: {cfg.originals_dir}  (read-only, mode 0444)")
    _echo(f"  exports  : {cfg.exports_dir}")


@app.command()
def doctor() -> None:
    """Report config paths and external-tool capabilities."""
    cfg = config.get_config()
    _echo(_bold("Configuration"))
    _echo(f"  data dir        : {cfg.data_dir}")
    _echo(f"  operator        : {cfg.operator}")
    _echo(f"  wayback enabled : {cfg.wayback_enabled}")
    _echo(f"  archivebox      : {cfg.archivebox_enabled}")
    _echo()
    _echo(_bold("Capabilities"))
    for name, cap in config.capabilities().items():
        mark = "ok " if cap.available else "MISS"
        ver = f"  ({cap.version})" if cap.version else ""
        _echo(f"  [{mark}] {name}{ver}")
    missing = [n for n, c in config.capabilities().items() if not c.available]
    if missing:
        _echo()
        _echo("Missing tools degrade gracefully; affected steps are skipped and")
        _echo("recorded in the custody log. Install for full capability:")
        _echo(f"  {', '.join(missing)}")


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------
@app.command()
def add(
    target: str = typer.Argument(..., help="A single URL or local file path."),
    location: str = typer.Option(None, "--location", help="Claimed location."),
    datetime_: str = typer.Option(None, "--datetime", help="Claimed datetime (ISO-8601)."),
    note: str = typer.Option(None, "--note", help="Operator note / description."),
    tags: str = typer.Option(None, "--tags", help="Comma-separated tags."),
    graphic: bool = typer.Option(False, "--graphic", help="Flag graphic content."),
    keyframes: int = typer.Option(5, "--keyframes", help="Keyframes per video (0 = none)."),
    make_thumbnails: bool = typer.Option(
        False, "--make-thumbnails",
        help="Opt in to keyframe generation even for graphic-flagged items."),
    uploader_handle: str = typer.Option(
        None, "--uploader-handle",
        help="SENSITIVE. Stored in an access-controlled field; never in the default export."),
    contributor_note: str = typer.Option(
        None, "--contributor-note", help="SENSITIVE. Access-controlled."),
    operator: str = typer.Option(None, "--operator", help="Override operator identity."),
    no_wayback: bool = typer.Option(False, "--no-wayback", help="Skip the Wayback snapshot."),
) -> None:
    """Ingest one operator-supplied item: capture, snapshot, hash, log."""
    kind = _classify_target(target)
    cfg = _effective_cfg(operator=operator, no_wayback=no_wayback)
    db.init_db(cfg)
    actor = cfg.operator

    with db.connect(cfg) as conn:
        item_id = db.insert_item(
            conn,
            ingested_by=actor,
            source_url=target if kind == "url" else None,
            source_kind=kind,
            platform=capture.detect_platform(target) if kind == "url" else "local-file",
            claimed_location=location,
            claimed_datetime=datetime_,
            description=note,
            tags=tags,
            graphic_flag=graphic,
        )
        db.log_custody(conn, item_id=item_id, actor=actor, action="create_item",
                       detail={"source_kind": kind, "graphic": graphic})

        # Sensitive identity data, if any, goes to the isolated table only.
        if uploader_handle or contributor_note:
            db.set_sensitive(conn, item_id=item_id, uploader_handle=uploader_handle,
                             contributor_note=contributor_note, recorded_by=actor)
            # Log that we recorded it, but never the value itself.
            db.log_custody(conn, item_id=item_id, actor=actor,
                           action="record_sensitive",
                           detail={"fields": [k for k, v in
                                   (("uploader_handle", uploader_handle),
                                    ("contributor_note", contributor_note)) if v]})

        try:
            summary = capture.ingest(
                conn, cfg, item_id=item_id, source=target, source_kind=kind,
                actor=actor, graphic=graphic, keyframes_n=keyframes,
                make_thumbnails=make_thumbnails,
            )
        except (FileNotFoundError, ValueError) as e:
            # The whole transaction rolls back on raise, so the item row and any
            # custody entries vanish; clean up the on-disk files too.
            capture.purge_item_dir(cfg, item_id)
            raise typer.BadParameter(str(e))
        except Exception:
            capture.purge_item_dir(cfg, item_id)
            raise

        db.set_status(conn, item_id, "captured")

    _echo(f"Added item {_bold('#' + str(item_id))}  ({kind})")
    for f in summary.files:
        _echo(f"  {f['role']:8s} {f['media_type']:6s} {f['sha256'][:16]}...  {f['path']}")
    if kind == "url":
        wb = summary.wayback_url or "(none - see custody log)"
        _echo(f"  wayback : {wb}")
    for w in summary.warnings:
        _echo(f"  note    : {w}")
    if not summary.files:
        _echo("  no files stored. Check 'doctor' for missing tools or the URL.")


# ---------------------------------------------------------------------------
# list / show
# ---------------------------------------------------------------------------
@app.command("list")
def list_cmd(
    status: str = typer.Option(None, "--status", help="Filter by status."),
    tag: str = typer.Option(None, "--tag", help="Filter by tag."),
    since: str = typer.Option(None, "--since", help="ingest_ts >= this ISO timestamp."),
) -> None:
    """List items with light filtering."""
    cfg = config.get_config()
    db.init_db(cfg)
    clauses, params = [], []
    if status:
        clauses.append("status = ?"); params.append(status)
    if tag:
        clauses.append("(',' || tags || ',') LIKE ?"); params.append(f"%,{tag.strip().lower()},%")
    if since:
        clauses.append("ingest_ts >= ?"); params.append(since)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with db.connect(cfg) as conn:
        rows = conn.execute(
            f"""SELECT i.*, (SELECT COUNT(*) FROM files f WHERE f.item_id = i.id) AS nfiles,
                       (SELECT verdict FROM verifications v WHERE v.item_id = i.id
                        ORDER BY v.id DESC LIMIT 1) AS verdict
                FROM items i{where} ORDER BY i.id DESC""",
            params,
        ).fetchall()
    if not rows:
        _echo("No items match.")
        return
    _echo(f"{'ID':>4}  {'STATUS':10} {'GFX':3} {'FILES':>5}  {'VERDICT':12} {'PLATFORM':10} INGESTED")
    for r in rows:
        gfx = "!" if r["graphic_flag"] else ""
        _echo(f"{r['id']:>4}  {r['status']:10} {gfx:3} {r['nfiles']:>5}  "
              f"{(r['verdict'] or 'unverified'):12} {(r['platform'] or '-'):10} {r['ingest_ts']}")


@app.command()
def show(
    item_id: int = typer.Argument(..., help="Item id."),
    show_sensitive: bool = typer.Option(
        False, "--show-sensitive",
        help="Reveal access-controlled identity fields (logged to custody)."),
    log_tail: int = typer.Option(10, "--log-tail", help="Custody entries to show."),
) -> None:
    """Show one item in full: files, captures, verifications, links, custody."""
    cfg = config.get_config()
    db.init_db(cfg)
    with db.connect(cfg) as conn:
        it = db.get_item(conn, item_id)
        if not it:
            _echo(f"No item #{item_id}.")
            raise typer.Exit(code=1)
        _echo(_bold(f"Item #{it['id']}  [{it['status']}]"))
        for k in ("ingest_ts", "ingested_by", "source_kind", "source_url",
                  "platform", "claimed_location", "claimed_datetime", "tags"):
            if it[k]:
                _echo(f"  {k:16}: {it[k]}")
        if it["graphic_flag"]:
            _echo(f"  {'graphic_flag':16}: {_bold('YES')}")
        if it["description"]:
            _echo(f"  {'description':16}: {it['description']}")

        files = db.get_files(conn, item_id)
        _echo(_bold(f"\n  Files ({len(files)})"))
        for f in files:
            ph = f" phash={f['phash']}" if f["phash"] else ""
            _echo(f"    [{f['role']}/{f['media_type']}] {f['path']}")
            _echo(f"       sha256={f['sha256']}{ph} bytes={f['bytes']}")

        caps = conn.execute(
            "SELECT * FROM captures WHERE item_id = ? ORDER BY id", (item_id,)
        ).fetchall()
        if caps:
            _echo(_bold(f"\n  Captures ({len(caps)})"))
            for c in caps:
                extra = c["wayback_url"] or c["warc_path"] or ""
                _echo(f"    {c['method']:12} {c['status']:8} {c['capture_ts']}  {extra}")

        vers = conn.execute(
            "SELECT * FROM verifications WHERE item_id = ? ORDER BY id", (item_id,)
        ).fetchall()
        if vers:
            _echo(_bold(f"\n  Verifications ({len(vers)})"))
            for v in vers:
                _echo(f"    {v['verdict']:12} by {v['verifier'] or '-'} "
                      f"via {v['method'] or '-'} @ {v['verified_ts']}")
                if v["notes"]:
                    _echo(f"       {v['notes']}")

        links = dedup.links_for(conn, item_id)
        if links:
            _echo(_bold(f"\n  Linked items ({len(links)})"))
            for ln in links:
                other = ln["other_id"] if ln["item_id"] == item_id else ln["item_id"]
                d = f" (distance {ln['distance']})" if ln["distance"] is not None else ""
                _echo(f"    {ln['relation']} -> #{other}{d}")

        if show_sensitive:
            srow = conn.execute(
                "SELECT * FROM item_sensitive WHERE item_id = ?", (item_id,)
            ).fetchone()
            db.log_custody(conn, item_id=item_id, actor=cfg.operator,
                           action="access_sensitive", detail="show --show-sensitive")
            _echo(_bold("\n  Sensitive (access-controlled)"))
            if srow:
                _echo(f"    uploader_handle : {srow['uploader_handle']}")
                _echo(f"    contributor_note: {srow['contributor_note']}")
                _echo(f"    recorded_by/ts  : {srow['recorded_by']} @ {srow['recorded_ts']}")
            else:
                _echo("    (none recorded)")

        log = conn.execute(
            "SELECT * FROM custody_log WHERE item_id = ? ORDER BY id DESC LIMIT ?",
            (item_id, log_tail),
        ).fetchall()
        if log:
            _echo(_bold(f"\n  Custody log (last {len(log)})"))
            for e in reversed(log):
                _echo(f"    {e['ts']}  {e['actor']:14} {e['action']}")


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------
@app.command()
def verify(
    item_id: int = typer.Argument(...),
    verdict: str = typer.Option(..., "--verdict", help="e.g. confirmed | inconclusive | disputed."),
    method: str = typer.Option(None, "--method", help="How the analyst verified."),
    notes: str = typer.Option(None, "--notes"),
    verifier: str = typer.Option(None, "--verifier", help="Analyst identity."),
) -> None:
    """Open a verification record (the tool supports verification; humans do it)."""
    cfg = config.get_config()
    db.init_db(cfg)
    with db.connect(cfg) as conn:
        if not db.get_item(conn, item_id):
            _echo(f"No item #{item_id}.")
            raise typer.Exit(code=1)
        db.add_verification_row(conn, item_id=item_id, verifier=verifier or cfg.operator,
                                verdict=verdict, method=method, notes=notes)
        db.set_status(conn, item_id, f"verified:{verdict}")
        db.log_custody(conn, item_id=item_id, actor=verifier or cfg.operator,
                       action="verify", detail={"verdict": verdict, "method": method})
    _echo(f"Recorded verification for #{item_id}: {_bold(verdict)}")


# ---------------------------------------------------------------------------
# check (integrity re-verification)
# ---------------------------------------------------------------------------
@app.command()
def check(
    item_id: int = typer.Argument(None, help="Item id, or omit to check all items."),
) -> None:
    """Recompute SHA-256 for stored files and report match / mismatch / missing."""
    cfg = config.get_config()
    db.init_db(cfg)
    ok = bad = missing = 0
    with db.connect(cfg) as conn:
        if item_id is not None:
            files = conn.execute("SELECT * FROM files WHERE item_id = ? ORDER BY id",
                                 (item_id,)).fetchall()
        else:
            files = conn.execute("SELECT * FROM files ORDER BY item_id, id").fetchall()
        for f in files:
            abs_path = cfg.data_dir / f["path"]
            if not abs_path.exists():
                missing += 1
                _echo(f"  MISSING  #{f['item_id']} {f['path']}")
                db.log_custody(conn, item_id=f["item_id"], actor=cfg.operator,
                               action="integrity_check",
                               detail={"file": f["path"], "result": "missing"})
                continue
            actual = hashing.sha256_file(abs_path)
            if actual == f["sha256"]:
                ok += 1
            else:
                bad += 1
                _echo(f"  MISMATCH #{f['item_id']} {f['path']}")
                _echo(f"           expected {f['sha256']}")
                _echo(f"           actual   {actual}")
            db.log_custody(conn, item_id=f["item_id"], actor=cfg.operator,
                           action="integrity_check",
                           detail={"file": f["path"],
                                   "result": "ok" if actual == f["sha256"] else "mismatch"})
    _echo(f"\nChecked {ok + bad + missing} files: "
          f"{_bold(str(ok))} ok, {bad} mismatch, {missing} missing.")
    if bad or missing:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# dedup
# ---------------------------------------------------------------------------
@app.command("dedup")
def dedup_cmd(
    threshold: int = typer.Option(hashing.DEFAULT_PHASH_THRESHOLD, "--threshold",
                                  help="Max pHash Hamming distance for a near match."),
) -> None:
    """Link exact (SHA-256) and near-duplicate (pHash) media across items."""
    cfg = config.get_config()
    db.init_db(cfg)
    with db.connect(cfg) as conn:
        result = dedup.run_dedup(conn, threshold=threshold)
    _echo(f"Scanned {result['files_scanned']} files (threshold {result['threshold']}).")
    _echo(f"  new exact links: {_bold(str(result['exact_links']))}")
    _echo(f"  new near links : {_bold(str(result['near_links']))}")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
@app.command("export")
def export_cmd(
    format: str = typer.Option("json", "--format", help="json | csv | bundle."),
    since: str = typer.Option(None, "--since", help="Only items with ingest_ts >= this."),
    out: Path = typer.Option(None, "--out", help="Output path (file or bundle dir)."),
    redact_source: bool = typer.Option(
        False, "--redact-source",
        help="json/csv: strip source/Wayback URLs and staff identity for circulation."),
    internal: bool = typer.Option(
        False, "--internal",
        help="bundle only: retain source URLs and staff identity (in-boundary use; "
             "a bundle redacts both by default)."),
    include_sensitive: bool = typer.Option(
        False, "--include-sensitive",
        help="Include access-controlled identities (logged to custody)."),
    no_media: bool = typer.Option(False, "--no-media",
                                  help="Bundle only: omit copies of originals."),
) -> None:
    """Export a JSON manifest, CSV, or a portable bundle.

    A bundle is fail-safe: source/Wayback URLs and staff identity are redacted
    by default (use --internal to retain them for in-boundary use). A plain
    json/csv retains URLs unless --redact-source.
    """
    cfg = config.get_config()
    db.init_db(cfg)
    fmt = format.lower()
    with db.connect(cfg) as conn:
        if include_sensitive:
            db.log_custody(conn, item_id=None, actor=cfg.operator,
                           action="export_with_sensitive",
                           detail={"format": fmt, "since": since})
        if fmt == "json":
            path = export.export_json(conn, cfg, since=since,
                                      include_sensitive=include_sensitive,
                                      redact_source=redact_source,
                                      redact_identity=redact_source, out=out)
        elif fmt == "csv":
            path = export.export_csv(conn, cfg, since=since,
                                     redact_source=redact_source, out=out)
        elif fmt == "bundle":
            path = export.export_bundle(conn, cfg, since=since,
                                        include_sensitive=include_sensitive,
                                        redact_source=not internal,
                                        redact_identity=not internal,
                                        include_media=not no_media, out=out)
        else:
            raise typer.BadParameter("format must be json, csv, or bundle")
    _echo(f"Wrote {fmt} export: {_bold(str(path))}")
    if fmt in ("json", "csv") and not redact_source:
        _echo("  WARNING: retains source URLs (and, for json, operator/analyst")
        _echo("           identity) that can identify an uploader. Use --redact-source")
        _echo("           for material that will circulate, or export a bundle")
        _echo("           (redacted by default).")
    if fmt == "bundle" and internal:
        _echo("  WARNING: --internal bundle retains source URLs and staff identity;")
        _echo("           for in-boundary use only, not wider circulation.")
    if include_sensitive:
        _echo("  WARNING: this export includes access-controlled identities.")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
