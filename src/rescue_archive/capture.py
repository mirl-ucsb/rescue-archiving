"""Capture stage: acquire bytes, freeze them read-only, snapshot independently.

This module is the pipeline coordinator for ``add``. It:
  1. acquires media (local-file copy, or yt-dlp / gallery-dl download),
  2. freezes every stored original read-only (0444) and hashes it on ingest,
  3. requests an independent Wayback Machine snapshot for web items,
  4. optionally drives ArchiveBox for a WARC page snapshot,
  5. extracts EXIF sidecars and video keyframes (provenance, not display),
  6. registers files + captures and writes a custody entry for every action.

Guardrails honoured here:
  * Human-supplied input only. We acquire exactly the one operator-supplied
    item and never expand a feed/profile: yt-dlp uses ``--no-playlist`` and
    gallery-dl uses ``--range 1``. Multi-file results above a cap are flagged.
  * No credentialed access. We pass no cookies/auth AND actively refuse to load
    ambient user config: yt-dlp gets ``--ignore-config`` and gallery-dl gets
    ``--config-ignore`` so a stray ~/.config credential cannot leak in.
  * Integrity. Originals are never re-encoded; we freeze and hash, then leave
    the bytes untouched (and verify the freeze took). Keyframes are derived.
  * Graphic content. Keyframe/thumbnail generation for graphic-flagged items
    is skipped unless the operator opts in (``make_thumbnails=True``).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from . import config, db, hashing, metadata

# A single operator-supplied post may legitimately hold several files (e.g. a
# multi-image post). More than this many media originals from one ``add`` is
# treated as a likely feed/profile expansion and flagged loudly in custody.
MULTI_FILE_CAP = 20


@dataclass
class IngestSummary:
    item_id: int
    files: list[dict] = field(default_factory=list)
    wayback_url: str | None = None
    warc_path: str | None = None
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def freeze_readonly(path: str | Path) -> None:
    """Make a stored original read-only. Never raises on odd filesystems."""
    config._chmod_quiet(path, config.ORIGINAL_MODE)


def purge_item_dir(cfg: config.Config, item_id: int) -> None:
    """Remove an item's on-disk directory, including read-only originals.

    Used to clean up after a rolled-back ingest so orphaned bytes never linger
    or get mixed into a later attempt that reuses the same (AUTOINCREMENT) id.
    """
    d = cfg.item_dir(item_id)
    if not d.exists():
        return
    for p in d.rglob("*"):
        if p.is_file():
            config._chmod_quiet(p, 0o600)  # re-grant write so rmtree can unlink
    shutil.rmtree(d, ignore_errors=True)


def _verify_frozen(conn, cfg, item_id, path: Path, actor, summary) -> None:
    """Confirm the read-only freeze actually took on POSIX; warn if not.

    A silent chmod failure (ACL conflict, odd mount) would otherwise leave a
    writable file recorded as a frozen original, weakening guardrail 2.
    """
    if os.name != "posix":
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return
    if mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        rel = _rel(cfg, path)
        summary.warnings.append(f"WARNING: original not read-only after freeze: {rel}")
        db.log_custody(conn, item_id=item_id, actor=actor,
                       action="freeze_unverified",
                       detail={"file": rel, "mode": oct(mode)})


def detect_platform(url: str | None) -> str | None:
    if not url:
        return None
    host = (urlparse(url).hostname or "").lower().lstrip("www.")
    table = {
        "youtube.com": "youtube", "youtu.be": "youtube",
        "twitter.com": "x", "x.com": "x",
        "instagram.com": "instagram", "facebook.com": "facebook",
        "fb.watch": "facebook", "tiktok.com": "tiktok",
        "t.me": "telegram", "telegram.me": "telegram",
        "reddit.com": "reddit", "bsky.app": "bluesky",
    }
    for domain, name in table.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host or None


def _rel(cfg: config.Config, path: Path) -> str:
    """Path relative to data_dir for portable storage in the DB/manifest."""
    try:
        return str(Path(path).resolve().relative_to(cfg.data_dir.resolve()))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Acquisition backends
# ---------------------------------------------------------------------------
def _run_ytdlp(url: str, dest_dir: Path) -> tuple[int, str]:
    """Download a single operator-supplied item. No auth, no playlist crawl."""
    out_tmpl = str(dest_dir / "%(id)s.%(ext)s")
    cmd = [
        "yt-dlp",
        "--ignore-config",        # no ambient user config (could inject cookies/auth)
        "--no-playlist",          # single item only; never expand a feed
        "--no-progress",
        "--no-overwrites",
        "--no-warnings",
        "--restrict-filenames",
        "--write-info-json",      # provenance: platform-reported metadata
        "--no-write-thumbnail",   # thumbnails handled under graphic-content policy
        "-o", out_tmpl,
        url,
    ]
    return _run(cmd)


def _run_gallery_dl(url: str, dest_dir: Path) -> tuple[int, str]:
    # Mirror the yt-dlp guarantees on the gallery-dl path:
    #   --config-ignore  : refuse ambient ~/.config/gallery-dl creds/cookies
    #   --range 1        : single item only; do not expand a profile/feed
    #   --filename ...   : identity-free, deterministic names (no uploader handle)
    cmd = [
        "gallery-dl",
        "--config-ignore",
        "--range", "1",
        "--no-mtime",
        "--filename", "{num:>04}.{extension}",
        "-D", str(dest_dir),
        url,
    ]
    return _run(cmd)


def _run(cmd: list[str], timeout: int = 1800) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return proc.returncode, "\n".join(tail[-8:])
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]} timed out after {timeout}s"
    except OSError as e:
        return 1, str(e)


def wayback_save(cfg: config.Config, url: str) -> tuple[str | None, str, str]:
    """Request an independent Wayback Machine snapshot.

    Returns (wayback_url, status, detail). Failure is non-fatal: the item is
    still captured locally; we just record that the independent copy failed so
    an operator can retry.
    """
    if not cfg.wayback_enabled:
        return None, "skipped", "wayback disabled in config"
    try:
        import requests  # type: ignore
    except Exception:
        return None, "failed", "requests library not installed"
    try:
        resp = requests.get(
            cfg.wayback_endpoint + url,
            timeout=cfg.wayback_timeout,
            allow_redirects=True,
            headers={"User-Agent": "rescue-archive/0.1 (+counter-archival capture)"},
        )
        loc = resp.headers.get("Content-Location") or resp.headers.get("content-location")
        if loc:
            return "https://web.archive.org" + loc, "ok", f"http {resp.status_code}"
        if resp.url and "/web/" in resp.url:
            return resp.url, "ok", f"http {resp.status_code}"
        return None, "failed", f"no snapshot url in response (http {resp.status_code})"
    except Exception as e:  # network errors, timeouts
        return None, "failed", f"{type(e).__name__}: {e}"


def archivebox_snapshot(cfg: config.Config, url: str) -> tuple[str | None, str, str]:
    """Best-effort WARC capture via ArchiveBox (optional, off by default)."""
    if not cfg.archivebox_enabled:
        return None, "skipped", "archivebox disabled in config"
    if not config.has("archivebox"):
        return None, "failed", "archivebox binary not found"
    ab_dir = cfg.data_dir / "archivebox"
    ab_dir.mkdir(parents=True, exist_ok=True)
    if not (ab_dir / "index.sqlite3").exists():
        _run(["archivebox", "init", "--setup"], timeout=300)  # idempotent-ish
    code, detail = _run(["archivebox", "add", "--depth=0", url], timeout=1800)
    status = "ok" if code == 0 else "failed"
    return (str(ab_dir) if code == 0 else None), status, detail


# ---------------------------------------------------------------------------
# Main coordinator
# ---------------------------------------------------------------------------
def ingest(
    conn,
    cfg: config.Config,
    *,
    item_id: int,
    source: str,
    source_kind: str,           # 'file' | 'url'
    actor: str,
    graphic: bool,
    keyframes_n: int = 5,
    make_thumbnails: bool = False,
) -> IngestSummary:
    cfg.ensure_dirs()
    item_dir = cfg.item_dir(item_id)
    # A non-empty item_dir for a fresh id means a prior attempt rolled back its
    # DB row (AUTOINCREMENT reuses the id) but left files on disk. Clear those
    # orphans so we never mix a failed attempt's bytes into this one.
    if item_dir.exists() and any(item_dir.iterdir()):
        purge_item_dir(cfg, item_id)
    item_dir.mkdir(parents=True, exist_ok=True)
    summary = IngestSummary(item_id=item_id)

    # --- 1. Acquire bytes -------------------------------------------------
    if source_kind == "file":
        saved = _ingest_local_file(conn, cfg, item_id, source, actor, summary)
    else:
        saved = _ingest_url(conn, cfg, item_id, source, actor, summary)

    # --- 2. Hash + register every stored original ------------------------
    # A directly operator-supplied file is always an 'original', even if its
    # extension maps to 'info' (a standalone .txt/.json/.srt is a real item,
    # not a co-downloaded sidecar).
    for path in saved:
        _register_original(conn, cfg, item_id, path, actor, summary,
                           force_original=(source_kind == "file"))

    # --- 3. Derived provenance: EXIF sidecars + video keyframes ----------
    for path in saved:
        mtype = metadata.media_type_for(path)
        if mtype == "image":
            _maybe_write_exif_sidecar(conn, cfg, item_id, path, actor, summary)
        elif mtype == "video":
            if graphic and not make_thumbnails:
                db.log_custody(conn, item_id=item_id, actor=actor,
                               action="keyframes_skipped",
                               detail="graphic_flag set; thumbnails not opted in")
                summary.warnings.append("keyframes skipped (graphic flag)")
            else:
                _extract_and_register_keyframes(
                    conn, cfg, item_id, path, actor, keyframes_n, summary
                )

    # --- 4. Independent snapshot (web items only) ------------------------
    if source_kind == "url":
        wb_url, wb_status, wb_detail = wayback_save(cfg, source)
        summary.wayback_url = wb_url
        db.add_capture_row(conn, item_id=item_id, method="wayback",
                           wayback_url=wb_url, tool="wayback-save-api",
                           status=wb_status, detail=wb_detail)
        db.log_custody(conn, item_id=item_id, actor=actor, action="wayback_save",
                       detail={"status": wb_status, "url": wb_url, "info": wb_detail})

        warc, ab_status, ab_detail = archivebox_snapshot(cfg, source)
        if ab_status != "skipped":
            summary.warc_path = warc
            db.add_capture_row(conn, item_id=item_id, method="archivebox",
                               warc_path=warc, tool="archivebox",
                               tool_version=config.tool_version("archivebox"),
                               status=ab_status, detail=ab_detail)
            db.log_custody(conn, item_id=item_id, actor=actor, action="archivebox",
                           detail={"status": ab_status, "warc": warc})

    return summary


# ---------------------------------------------------------------------------
# Acquisition implementations
# ---------------------------------------------------------------------------
def _ingest_local_file(conn, cfg, item_id, source, actor, summary) -> list[Path]:
    src = Path(source).expanduser()
    if src.is_dir():
        raise ValueError(
            "directory ingest is out of scope: supply a single file or URL"
        )
    if not src.is_file():
        raise FileNotFoundError(f"no such file: {src}")
    dst = cfg.item_dir(item_id) / src.name
    shutil.copy2(src, dst)            # preserve mtime; never move the source
    freeze_readonly(dst)
    db.add_capture_row(conn, item_id=item_id, method="file-ingest",
                       tool="cp", status="ok",
                       detail=f"copied from operator-supplied path")
    db.log_custody(conn, item_id=item_id, actor=actor, action="ingest_file",
                   detail={"original_filename": src.name})
    return [dst]


def _ingest_url(conn, cfg, item_id, url, actor, summary) -> list[Path]:
    item_dir = cfg.item_dir(item_id)
    before = set(item_dir.iterdir()) if item_dir.exists() else set()
    method = "yt-dlp"
    code, detail = (127, "yt-dlp not found")

    if config.has("yt-dlp"):
        code, detail = _run_ytdlp(url, item_dir)
    produced = sorted(p for p in item_dir.iterdir() if p not in before)

    def media(paths):
        return [p for p in paths if metadata.media_type_for(p) in ("video", "image", "audio")]

    # If yt-dlp produced no media (e.g. an image gallery URL), try gallery-dl.
    if not media(produced) and config.has("gallery-dl"):
        method = "gallery-dl"
        g_code, g_detail = _run_gallery_dl(url, item_dir)
        code, detail = g_code, g_detail
        produced = sorted(p for p in item_dir.iterdir() if p not in before)

    # Success requires BOTH a clean downloader exit AND at least one real media
    # file. An info-json-only result (yt-dlp writes it even when the stream is
    # geo/age/members-restricted or fails mid-download) is NOT a success.
    media_files = media(produced)
    if code == 0 and media_files:
        status = "ok"
    elif media_files:
        status = "partial"   # got media but downloader reported a non-zero exit
    elif produced:
        status = "metadata-only"  # sidecars only, no media bytes
    else:
        status = "failed"

    db.add_capture_row(conn, item_id=item_id, method=method, tool=method,
                       tool_version=config.tool_version(method),
                       status=status, detail=f"exit={code}; {detail}")
    db.log_custody(conn, item_id=item_id, actor=actor, action="download",
                   detail={"method": method, "status": status, "exit": code,
                           "files": len(produced), "media": len(media_files),
                           "info": detail})
    if status != "ok":
        summary.warnings.append(f"capture {status} ({method} exit={code}): {detail}")

    # Guardrail 1 backstop: a single operator item should not yield a feed.
    if len(media_files) > MULTI_FILE_CAP:
        msg = (f"{len(media_files)} media files from one item: possible feed/"
               f"profile expansion; review before trusting this capture")
        summary.warnings.append(msg)
        db.log_custody(conn, item_id=item_id, actor=actor,
                       action="multi_file_flag",
                       detail={"media_files": len(media_files), "cap": MULTI_FILE_CAP})

    # Freeze everything we captured, including the info JSON sidecar.
    for p in produced:
        freeze_readonly(p)
    return produced


# ---------------------------------------------------------------------------
# Registration + derived artifacts
# ---------------------------------------------------------------------------
def _register_original(conn, cfg, item_id, path: Path, actor, summary,
                       force_original: bool = False) -> None:
    _verify_frozen(conn, cfg, item_id, path, actor, summary)
    sha = hashing.sha256_file(path)
    mtype = metadata.media_type_for(path)
    role = "original" if force_original else ("sidecar" if mtype in ("info",) else "original")
    phash = hashing.phash_image(path) if mtype == "image" else None
    size = path.stat().st_size
    db.add_file_row(conn, item_id=item_id, rel_path=_rel(cfg, path),
                    media_type=mtype, role=role, sha256=sha, phash=phash,
                    bytes_=size, original_filename=path.name)
    db.log_custody(conn, item_id=item_id, actor=actor, action="hash",
                   detail={"file": _rel(cfg, path), "sha256": sha,
                           "phash": phash, "bytes": size})
    summary.files.append({"path": _rel(cfg, path), "sha256": sha,
                          "phash": phash, "media_type": mtype, "role": role})


def _maybe_write_exif_sidecar(conn, cfg, item_id, path: Path, actor, summary) -> None:
    exif = metadata.extract_exif(path)
    if not exif:
        return
    import json
    sidecar = cfg.item_dir(item_id) / f"{path.name}.exif.json"
    sidecar.write_text(json.dumps(exif, ensure_ascii=False, indent=2, default=str))
    freeze_readonly(sidecar)
    # EXIF can carry GPS/author/serials: store + hash, but keep out of default export.
    _register_original(conn, cfg, item_id, sidecar, actor, summary)
    db.log_custody(conn, item_id=item_id, actor=actor, action="exif_extracted",
                   detail={"file": _rel(cfg, path), "fields": len(exif)})


def _extract_and_register_keyframes(conn, cfg, item_id, video: Path, actor, n, summary) -> None:
    kf_dir = cfg.item_dir(item_id) / "keyframes"
    frames = metadata.extract_keyframes(video, kf_dir, n=n)
    for fr in frames:
        freeze_readonly(fr)
        sha = hashing.sha256_file(fr)
        phash = hashing.phash_image(fr)
        db.add_file_row(conn, item_id=item_id, rel_path=_rel(cfg, fr),
                        media_type="image", role="keyframe", sha256=sha,
                        phash=phash, bytes_=fr.stat().st_size,
                        original_filename=fr.name)
    db.log_custody(conn, item_id=item_id, actor=actor, action="keyframes_extracted",
                   detail={"video": _rel(cfg, video), "count": len(frames)})
    if frames:
        summary.warnings.append(f"{len(frames)} keyframes extracted")
