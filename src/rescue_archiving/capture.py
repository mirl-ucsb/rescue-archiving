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
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from . import config, db, hashing, metadata, ots, timestamp

# A single operator-supplied post may legitimately hold several files (e.g. a
# multi-image post). This many media originals from one ``add`` is the hard
# ceiling passed to gallery-dl itself (``--range 1-N``); reaching it is flagged
# loudly in custody. Override with RESCUE_ARCHIVING_MULTI_FILE_CAP.
MULTI_FILE_CAP = 20

# Subcategory names gallery-dl's own URL matcher uses for a SINGLE item (one
# post, one album, one file). Anything else (user, timeline, media, posts,
# search, tag, likes, ...) is a feed or profile and is never expanded. The names
# are consistent across gallery-dl's extractors, so this list is platform-agnostic.
SINGLE_POST_SUBCATEGORIES = frozenset({
    "post", "tweet", "submission", "status", "image", "photo", "picture", "file",
    "album", "gallery", "set", "video", "vmpost", "redirect", "item", "deviation",
    "artwork",
})

# Wayback Save Page Now is rate-limited and sometimes down: retry transient
# failures with a short back-off, then fall back to any existing snapshot.
WAYBACK_RETRY_DELAYS = (0, 8, 20)
WAYBACK_RETRYABLE = (429, 500, 502, 503, 504)
WAYBACK_AVAILABLE = "https://archive.org/wayback/available"


def classify_url(url: str) -> tuple[str, str] | None:
    """(category, subcategory) from gallery-dl's own URL matcher, offline.

    None when the ``gallery_dl`` module is not importable or nothing matches;
    callers treat None conservatively (first item only).
    """
    try:
        from gallery_dl import extractor  # type: ignore
    except Exception:
        return None
    try:
        ex = extractor.find(url)
    except Exception:
        return None
    if ex is None:
        return None
    return (str(ex.category), str(ex.subcategory))


def is_single_post(classification: tuple[str, str] | None) -> bool:
    return bool(classification) and classification[1] in SINGLE_POST_SUBCATEGORIES


@dataclass
class IngestSummary:
    item_id: int
    files: list[dict] = field(default_factory=list)
    wayback_url: str | None = None
    warc_path: str | None = None
    warnings: list[str] = field(default_factory=list)
    stamps: list[dict] = field(default_factory=list)   # RFC 3161 / OTS results per file
    classification: str | None = None                  # gallery-dl 'category.subcategory'
    capture_mode: str | None = None                    # 'whole-post' | 'single-item'


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
        config.tool_path("yt-dlp") or "yt-dlp",
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


def _run_gallery_dl(url: str, dest_dir: Path, whole_post: bool = False,
                    cap: int = MULTI_FILE_CAP) -> tuple[int, str]:
    # Mirror the yt-dlp guarantees on the gallery-dl path:
    #   --config-ignore  : refuse ambient ~/.config/gallery-dl creds/cookies
    #   --range          : first item only by default; a link classified as a
    #                      single post may take the whole post, hard-capped so a
    #                      misclassified feed can never expand past ``cap``
    #   --filename ...   : identity-free, deterministic names (no uploader handle)
    rng = f"1-{cap}" if whole_post else "1"
    cmd = [
        config.tool_path("gallery-dl") or "gallery-dl",
        "--config-ignore",
        "--range", rng,
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

    Returns (wayback_url, status, detail). Transient failures (rate limits,
    server errors, network hiccups) are retried with a short back-off. Failure
    is non-fatal: the item is still captured locally; we record that the
    independent copy failed so ``retry-snapshots`` can try again later.
    """
    if not cfg.wayback_enabled:
        return None, "skipped", "wayback disabled in config"
    try:
        import requests  # type: ignore
    except Exception:
        return None, "failed", "requests library not installed"
    last = "no attempt"
    for delay in WAYBACK_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            resp = requests.get(
                cfg.wayback_endpoint + url,
                timeout=cfg.wayback_timeout,
                allow_redirects=True,
                headers={"User-Agent": "rescue-archiving (+counter-archival capture)"},
            )
        except Exception as e:  # network errors, timeouts: transient, retry
            last = f"{type(e).__name__}: {e}"
            continue
        loc = resp.headers.get("Content-Location") or resp.headers.get("content-location")
        if loc:
            return "https://web.archive.org" + loc, "ok", f"http {resp.status_code}"
        if resp.url and "/web/" in resp.url:
            return resp.url, "ok", f"http {resp.status_code}"
        last = f"no snapshot url in response (http {resp.status_code})"
        if resp.status_code not in WAYBACK_RETRYABLE:
            break                       # not transient (e.g. the URL cannot be archived)
    return None, "failed", last


def wayback_existing(cfg: config.Config, url: str) -> tuple[str | None, str | None]:
    """The closest EXISTING snapshot (url, timestamp) via the availability API,
    or (None, None). Used only after a fresh Save Page Now request has failed,
    so independent provenance degrades to 'an earlier copy exists' rather than
    to nothing."""
    try:
        import requests  # type: ignore
    except Exception:
        return None, None
    # The availability API matches loosely and answers most reliably for a bare
    # form (observed: an HTML 429 for 'https://example.com/' while 'example.com'
    # returned the snapshot), so try the URL as given, then without its scheme.
    bare = (url.split("://", 1)[1] if "://" in url else url).rstrip("/")
    for candidate in dict.fromkeys((url, bare)):
        try:
            r = requests.get(WAYBACK_AVAILABLE, params={"url": candidate}, timeout=30)
            closest = (r.json().get("archived_snapshots") or {}).get("closest") or {}
            if closest.get("available") and closest.get("url"):
                return closest["url"], closest.get("timestamp")
        except Exception:
            continue
    return None, None


def retry_snapshots(conn, cfg: config.Config, *, item_id: int | None = None,
                    actor: str) -> list[dict]:
    """Re-request a Wayback snapshot for web items whose latest attempt failed
    or found only an earlier snapshot. A deliberate skip (``--no-wayback``) is
    retried only when the item is named explicitly. Adds a new capture row and
    custody entry; never rewrites earlier ones."""
    q = "SELECT id, source_url FROM items WHERE source_kind = 'url' AND source_url IS NOT NULL"
    params: tuple = ()
    if item_id is not None:
        q += " AND id = ?"
        params = (item_id,)
    results = []
    for it in conn.execute(q + " ORDER BY id", params).fetchall():
        last = conn.execute(
            "SELECT status FROM captures WHERE item_id = ? AND method = 'wayback' "
            "ORDER BY id DESC LIMIT 1", (it["id"],)).fetchone()
        if last and last["status"] == "ok":
            continue
        # In a sweep, respect a deliberate --no-wayback skip; naming the item
        # is the operator saying "now try".
        if item_id is None and (last is None or last["status"] == "skipped"):
            continue
        wb, status, detail = wayback_save(cfg, it["source_url"])
        db.add_capture_row(conn, item_id=it["id"], method="wayback", wayback_url=wb,
                           tool="wayback-save-api", status=status, detail=f"retry: {detail}")
        db.log_custody(conn, item_id=it["id"], actor=actor, action="wayback_retry",
                       detail={"status": status, "url": wb, "info": detail})
        results.append({"item_id": it["id"], "status": status, "url": wb, "detail": detail})
    return results


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
    whole_post: bool = False,
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
        saved = _ingest_url(conn, cfg, item_id, source, actor, summary,
                            whole_post=whole_post)

    # --- 2. Hash + register every stored original ------------------------
    # A directly operator-supplied file is always an 'original', even if its
    # extension maps to 'info' (a standalone .txt/.json/.srt is a real item,
    # not a co-downloaded sidecar).
    for path in saved:
        _register_original(conn, cfg, item_id, path, actor, summary,
                           force_original=(source_kind == "file"))

    # --- 2b. Independent time attestation (RFC 3161) --------------------
    # Stamp what came from the source (originals and platform sidecars), not
    # the locally derived keyframes. Each stamp is a nonced commitment, so the
    # TSA never learns a file hash. Failure is recorded, never fatal.
    for f in list(summary.files):
        if f["role"] in ("original", "sidecar"):
            res = timestamp.stamp_file(conn, cfg, item_id=item_id,
                                       path=cfg.data_dir / f["path"],
                                       sha256_hex=f["sha256"], actor=actor)
            summary.stamps.append(res)
            if res["status"] == "failed":
                summary.warnings.append(
                    f"timestamp failed for {f['path']}: {res['detail']}")
            # Second, independent anchor: OpenTimestamps (Bitcoin). Pending
            # until upgrade-stamps completes it; skipped if the client is absent.
            ores = ots.stamp_file(conn, cfg, item_id=item_id,
                                  path=cfg.data_dir / f["path"],
                                  sha256_hex=f["sha256"], actor=actor)
            summary.stamps.append(ores)
            if ores["status"] == "failed":
                summary.warnings.append(
                    f"opentimestamps failed for {f['path']}: {ores['detail']}")

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
        if wb_status == "failed":
            # Fall back to an earlier third-party copy, clearly marked as such.
            ex_url, ex_ts = wayback_existing(cfg, source)
            if ex_url:
                wb_url, wb_status = ex_url, "existing"
                wb_detail = (f"fresh snapshot failed ({wb_detail}); earlier snapshot "
                             f"{ex_ts} found via the availability API")
                summary.warnings.append(
                    f"fresh Wayback snapshot failed; an earlier snapshot exists "
                    f"({ex_ts}); run retry-snapshots later for a fresh one")
                db.log_custody(conn, item_id=item_id, actor=actor,
                               action="wayback_existing",
                               detail={"url": ex_url, "timestamp": ex_ts})
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


def _ingest_url(conn, cfg, item_id, url, actor, summary,
                whole_post: bool = False) -> list[Path]:
    item_dir = cfg.item_dir(item_id)
    cap = cfg.multi_file_cap
    classification = mode = None
    before = set(item_dir.iterdir()) if item_dir.exists() else set()
    method = "yt-dlp"
    code, detail = (127, "yt-dlp not found")

    if config.has("yt-dlp"):
        code, detail = _run_ytdlp(url, item_dir)
    produced = sorted(p for p in item_dir.iterdir() if p not in before)

    def media(paths):
        return [p for p in paths if metadata.media_type_for(p) in ("video", "image", "audio")]

    # If yt-dlp produced no media (e.g. an image post), try gallery-dl. Ask
    # gallery-dl's own URL matcher first: a single item may be taken whole,
    # anything else stays at its first item. Guardrail 1: never expand a feed.
    if not media(produced) and config.has("gallery-dl"):
        method = "gallery-dl"
        cls = classify_url(url)
        classification = f"{cls[0]}.{cls[1]}" if cls else None
        whole = whole_post or is_single_post(cls)
        mode = "whole-post" if whole else "single-item"
        summary.classification, summary.capture_mode = classification, mode
        if not whole:
            summary.warnings.append(
                f"link classified as {classification or 'unclassified'} (a set, feed, "
                f"or unknown): captured the first item only; pass --whole-post to "
                f"override (capped at {cap})")
        g_code, g_detail = _run_gallery_dl(url, item_dir, whole_post=whole, cap=cap)
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
                           "classification": classification, "mode": mode,
                           "override": bool(whole_post), "info": detail})
    if status != "ok":
        summary.warnings.append(f"capture {status} ({method} exit={code}): {detail}")

    # Guardrail 1 backstop: reaching the hard cap means the post may hold more,
    # or the link was a feed after all. Either way, review before trusting it.
    if len(media_files) >= cap:
        msg = (f"{len(media_files)} media files from one item: reached the {cap}-file "
               f"cap; the post may hold more, or this may be a feed; review before "
               f"trusting this capture")
        summary.warnings.append(msg)
        db.log_custody(conn, item_id=item_id, actor=actor,
                       action="multi_file_flag",
                       detail={"media_files": len(media_files), "cap": cap})

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
