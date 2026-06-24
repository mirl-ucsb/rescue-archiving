"""Export: portable JSON manifest, CSV, and a file bundle.

Source-protection contract (enforced here):
  * The ``item_sensitive`` table (operator-flagged uploader handles and
    contributor notes) is NEVER read on the default path. It is included only
    when ``include_sensitive=True`` is passed explicitly, and that disclosure
    is recorded in the custody log by the caller.
  * ``redact_source=True`` strips source/Wayback/WARC URLs, and
    ``redact_identity=True`` strips operator/analyst identity (ingested_by,
    generated_by, verifier). A source URL on a handle-based platform IS the
    uploader's identity, so both matter for material that will circulate.
  * A ``bundle`` is the artifact that leaves the access-controlled boundary,
    so it redacts BOTH by default (fail-safe); pass an internal bundle
    (redact_source=redact_identity=False) only for in-boundary use. A plain
    json/csv retains URLs by default (provenance) unless redact_source is set.

EXIF sidecars are stored on disk but their payloads are not inlined into the
manifest; only the sidecar file's hash is listed. Derived ``.info.json`` /
``.exif.json`` sidecars are withheld from a bundle unless include_sensitive.
"""

from __future__ import annotations

import csv
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import config, db


def _is_sensitive_sidecar(f: dict) -> bool:
    """True for DERIVED sidecars that embed identity/location/source payloads.

    yt-dlp ``.info.json`` carries uploader handle + source URL; EXIF
    ``.exif.json`` carries GPS, author, and device serials. These live inside
    the access-controlled data tree but must not be copied into an export
    bundle that may leave that boundary, unless the operator opts in.

    Operator-supplied originals are always included, even a standalone
    ``.txt``/``.json`` document (which maps to media_type 'info'): only files
    derived alongside a capture (role 'sidecar') are withheld.
    """
    if f.get("role") == "original":
        return False
    return (
        f.get("role") == "sidecar"
        or str(f.get("path", "")).endswith((".exif.json", ".info.json"))
    )


def _fname_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _since_clause(since: str | None) -> tuple[str, tuple]:
    if since:
        return " WHERE ingest_ts >= ?", (since,)
    return "", ()


def build_manifest(
    conn,
    cfg: config.Config,
    *,
    since: str | None = None,
    include_sensitive: bool = False,
    redact_source: bool = False,
    redact_identity: bool = False,
) -> dict:
    where, params = _since_clause(since)
    item_rows = conn.execute(
        f"SELECT * FROM items{where} ORDER BY id", params
    ).fetchall()

    items = []
    for it in item_rows:
        iid = it["id"]
        files = [
            {
                "path": f["path"],
                "media_type": f["media_type"],
                "role": f["role"],
                "sha256": f["sha256"],
                "phash": f["phash"],
                "bytes": f["bytes"],
                "original_filename": f["original_filename"],
            }
            for f in db.get_files(conn, iid)
        ]
        captures = [
            {
                "method": c["method"],
                "capture_ts": c["capture_ts"],
                "wayback_url": None if redact_source else c["wayback_url"],
                "warc_path": None if redact_source else c["warc_path"],
                "tool": c["tool"],
                "tool_version": c["tool_version"],
                "status": c["status"],
            }
            for c in conn.execute(
                "SELECT * FROM captures WHERE item_id = ? ORDER BY id", (iid,)
            ).fetchall()
        ]
        verifications = [
            {
                "verifier": None if redact_identity else v["verifier"],
                "verified_ts": v["verified_ts"],
                "verdict": v["verdict"],
                "method": v["method"],
                "notes": v["notes"],
            }
            for v in conn.execute(
                "SELECT * FROM verifications WHERE item_id = ? ORDER BY id", (iid,)
            ).fetchall()
        ]
        links = [
            {"other_id": ln["other_id"] if ln["item_id"] == iid else ln["item_id"],
             "relation": ln["relation"], "distance": ln["distance"]}
            for ln in conn.execute(
                "SELECT * FROM item_links WHERE item_id = ? OR other_id = ?",
                (iid, iid),
            ).fetchall()
        ]
        entry = {
            "id": iid,
            "ingest_ts": it["ingest_ts"],
            "ingested_by": None if redact_identity else it["ingested_by"],
            "source_url": None if redact_source else it["source_url"],
            "source_kind": it["source_kind"],
            "platform": it["platform"],
            "claimed_location": it["claimed_location"],
            "claimed_datetime": it["claimed_datetime"],
            "description": it["description"],
            "status": it["status"],
            "tags": it["tags"].split(",") if it["tags"] else [],
            "graphic_flag": bool(it["graphic_flag"]),
            "verification_status": verifications[-1]["verdict"] if verifications else "unverified",
            "files": files,
            "captures": captures,
            "verifications": verifications,
            "links": links,
        }
        if include_sensitive:
            srow = conn.execute(
                "SELECT * FROM item_sensitive WHERE item_id = ?", (iid,)
            ).fetchone()
            entry["sensitive"] = (
                {"uploader_handle": srow["uploader_handle"],
                 "contributor_note": srow["contributor_note"],
                 "recorded_ts": srow["recorded_ts"],
                 "recorded_by": srow["recorded_by"]}
                if srow else None
            )
        items.append(entry)

    return {
        "manifest_version": 1,
        "tool": "rescue-archiving",
        "generated_ts": db.utcnow(),
        "generated_by": None if redact_identity else cfg.operator,
        "filters": {"since": since},
        "redactions": {
            "source_urls_redacted": redact_source,
            "identities_redacted": redact_identity,
            "sensitive_included": include_sensitive,
        },
        "item_count": len(items),
        "items": items,
    }


def export_json(conn, cfg, *, since=None, include_sensitive=False,
                redact_source=False, redact_identity=False,
                out: Path | None = None) -> Path:
    manifest = build_manifest(conn, cfg, since=since,
                              include_sensitive=include_sensitive,
                              redact_source=redact_source,
                              redact_identity=redact_identity)
    out = out or (cfg.exports_dir / f"manifest_{_fname_stamp()}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return out


CSV_COLUMNS = [
    "item_id", "ingest_ts", "platform", "claimed_location", "claimed_datetime",
    "status", "verification_status", "tags", "graphic_flag", "source_url",
    "file_path", "media_type", "role", "sha256", "phash", "bytes",
    "wayback_url",
]


def export_csv(conn, cfg, *, since=None, redact_source=False,
               out: Path | None = None) -> Path:
    manifest = build_manifest(conn, cfg, since=since, redact_source=redact_source)
    out = out or (cfg.exports_dir / f"manifest_{_fname_stamp()}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for it in manifest["items"]:
            wb = next((c["wayback_url"] for c in it["captures"]
                       if c["method"] == "wayback" and c["wayback_url"]), None)
            base = {
                "item_id": it["id"], "ingest_ts": it["ingest_ts"],
                "platform": it["platform"], "claimed_location": it["claimed_location"],
                "claimed_datetime": it["claimed_datetime"], "status": it["status"],
                "verification_status": it["verification_status"],
                "tags": "|".join(it["tags"]), "graphic_flag": it["graphic_flag"],
                "source_url": it["source_url"], "wayback_url": wb,
            }
            if it["files"]:
                for f in it["files"]:
                    w.writerow({**base, "file_path": f["path"],
                                "media_type": f["media_type"], "role": f["role"],
                                "sha256": f["sha256"], "phash": f["phash"],
                                "bytes": f["bytes"]})
            else:
                w.writerow(base)
    return out


def export_bundle(conn, cfg, *, since=None, include_sensitive=False,
                  redact_source=True, redact_identity=True, include_media=True,
                  out: Path | None = None) -> Path:
    """Produce a self-contained bundle directory under exports/.

    Contents: manifest.json, manifest.csv, SHA256SUMS (over copied originals),
    README.txt, and (optionally) read-only copies of the originals.

    Fail-safe by default: a bundle is the artifact that leaves the
    access-controlled boundary, so source/Wayback URLs (which can name an
    uploader) and staff identity (operator/analyst) are redacted unless the
    caller opts into an internal bundle with redact_source=redact_identity=False.
    """
    bundle = out or (cfg.exports_dir / f"bundle_{_fname_stamp()}")
    bundle.mkdir(parents=True, exist_ok=True)
    config._chmod_quiet(bundle, config.EXPORT_DIR_MODE)

    export_json(conn, cfg, since=since, include_sensitive=include_sensitive,
                redact_source=redact_source, redact_identity=redact_identity,
                out=bundle / "manifest.json")
    export_csv(conn, cfg, since=since, redact_source=redact_source,
               out=bundle / "manifest.csv")

    checksums: list[str] = []
    excluded_sidecars = 0
    if include_media:
        manifest = build_manifest(conn, cfg, since=since)
        media_root = bundle / "files"
        for it in manifest["items"]:
            for f in it["files"]:
                # Never copy identity/location/source-bearing sidecars into a
                # bundle that may leave the access-controlled boundary, unless
                # the operator explicitly opted in. The manifest still records
                # the sidecar's existence + hash for chain-of-custody.
                if _is_sensitive_sidecar(f) and not include_sensitive:
                    excluded_sidecars += 1
                    continue
                src = cfg.data_dir / f["path"]
                if not src.exists():
                    continue
                dst = media_root / f["path"]
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                config._chmod_quiet(dst, config.ORIGINAL_MODE)
                checksums.append(f"{f['sha256']}  files/{f['path']}")
        (bundle / "SHA256SUMS").write_text("\n".join(sorted(checksums)) + "\n")
        if excluded_sidecars:
            db.log_custody(conn, item_id=None, actor=cfg.operator,
                           action="bundle_excluded_sidecars",
                           detail={"count": excluded_sidecars})

    (bundle / "README.txt").write_text(
        _bundle_readme(redact_source, redact_identity, include_sensitive,
                       excluded_sidecars))
    return bundle


def _bundle_readme(redact_source: bool, redact_identity: bool,
                   include_sensitive: bool, excluded_sidecars: int = 0) -> str:
    lines = [
        "rescue-archiving export bundle",
        "==============================",
        "",
        "manifest.json  - full item/file/capture/verification manifest",
        "manifest.csv   - one row per file (flat)",
        "SHA256SUMS     - integrity checksums; verify with: shasum -c SHA256SUMS",
        "files/         - read-only copies of captured originals (if included)",
        "",
        f"source / Wayback URLs : {'redacted' if redact_source else 'RETAINED (a URL can name an uploader)'}",
        f"operator / analyst id : {'redacted' if redact_identity else 'RETAINED'}",
        f"flagged identities    : {'INCLUDED (access-controlled)' if include_sensitive else 'excluded'}",
    ]
    if include_sensitive:
        lines.append("sidecars              : info.json / exif.json copied (access-controlled)")
    else:
        lines.append(
            f"sidecars excluded     : {excluded_sidecars} "
            "(info.json / exif.json kept out; their hashes remain in the manifest)")
    lines.append("")
    lines.append("Handle this bundle as access-controlled material.")
    if redact_source and redact_identity and not include_sensitive:
        lines.append("This is a circulation-safe bundle: no source URLs, no staff")
        lines.append("identity, and no flagged contributor identities.")
    else:
        lines.append("WARNING: this bundle retains identifying information (see the")
        lines.append("flags above). Treat it as internal, not for wider circulation.")
    return "\n".join(lines) + "\n"
