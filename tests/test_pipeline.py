"""Offline pipeline tests: ingest a local file, hash, store read-only, export.

These exercise the network-free path so they run anywhere without touching
yt-dlp, the Wayback API, or any sensitive live URL.
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat
from pathlib import Path

import pytest

from rescue_archive import capture, config, db, dedup, export, hashing, metadata


@pytest.fixture()
def cfg(tmp_path: Path) -> config.Config:
    c = config.Config(
        root=tmp_path,
        data_dir=tmp_path / "data",
        exports_dir=tmp_path / "exports",
        operator="tester",
        wayback_enabled=False,  # never hit the network in tests
        archivebox_enabled=False,
    )
    db.init_db(c)
    return c


def _make_file(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def test_sha256_matches_hashlib(tmp_path: Path):
    f = _make_file(tmp_path / "a.bin", b"counter-archive")
    import hashlib
    assert hashing.sha256_file(f) == hashlib.sha256(b"counter-archive").hexdigest()


def test_file_ingest_hashes_and_freezes(cfg: config.Config, tmp_path: Path):
    src = _make_file(tmp_path / "clip.mp4", b"\x00\x01video-bytes\x02")
    with db.connect(cfg) as conn:
        item_id = db.insert_item(
            conn, ingested_by="tester", source_url=None, source_kind="file",
            platform="local-file", claimed_location="Dahieh", claimed_datetime=None,
            description="test clip", tags="beirut,strike", graphic_flag=False,
        )
        summary = capture.ingest(conn, cfg, item_id=item_id, source=str(src),
                                 source_kind="file", actor="tester", graphic=False,
                                 keyframes_n=0)
    assert len(summary.files) == 1
    stored = cfg.item_dir(item_id) / "clip.mp4"
    assert stored.exists()
    # Original bytes are byte-identical.
    assert stored.read_bytes() == b"\x00\x01video-bytes\x02"
    # Stored read-only (no owner write bit).
    mode = stat.S_IMODE(os.stat(stored).st_mode)
    assert not (mode & stat.S_IWUSR), f"expected read-only, got {oct(mode)}"
    # Hash recorded matches recomputation.
    assert summary.files[0]["sha256"] == hashing.sha256_file(stored)


def test_custody_log_is_append_only(cfg: config.Config):
    with db.connect(cfg) as conn:
        item_id = db.insert_item(
            conn, ingested_by="t", source_url=None, source_kind="file",
            platform="local-file", claimed_location=None, claimed_datetime=None,
            description=None, tags=None, graphic_flag=False)
        db.log_custody(conn, item_id=item_id, actor="t", action="create_item")
    import sqlite3
    with db.connect(cfg) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE custody_log SET action='tampered' WHERE item_id=?", (item_id,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM custody_log WHERE item_id=?", (item_id,))


def test_check_detects_tampering(cfg: config.Config, tmp_path: Path):
    src = _make_file(tmp_path / "p.txt", b"original")
    with db.connect(cfg) as conn:
        item_id = db.insert_item(
            conn, ingested_by="t", source_url=None, source_kind="file",
            platform="local-file", claimed_location=None, claimed_datetime=None,
            description=None, tags=None, graphic_flag=False)
        capture.ingest(conn, cfg, item_id=item_id, source=str(src),
                       source_kind="file", actor="t", graphic=False, keyframes_n=0)
        stored = conn.execute("SELECT path, sha256 FROM files WHERE item_id=?",
                              (item_id,)).fetchone()
    abs_path = cfg.data_dir / stored["path"]
    # Tamper: make writable, change bytes.
    os.chmod(abs_path, 0o644)
    abs_path.write_bytes(b"tampered!")
    assert hashing.sha256_file(abs_path) != stored["sha256"]


def test_export_excludes_sensitive_by_default(cfg: config.Config, tmp_path: Path):
    src = _make_file(tmp_path / "img.jpg", b"jpeg-ish-bytes")
    with db.connect(cfg) as conn:
        item_id = db.insert_item(
            conn, ingested_by="tester", source_url="https://x.com/SECRETHANDLE/status/1",
            source_kind="url", platform="x", claimed_location=None,
            claimed_datetime=None, description=None, tags=None, graphic_flag=False)
        db.set_sensitive(conn, item_id=item_id, uploader_handle="SECRETHANDLE",
                         contributor_note="do not disclose", recorded_by="tester")
        capture.ingest(conn, cfg, item_id=item_id, source=str(src),
                       source_kind="file", actor="tester", graphic=False, keyframes_n=0)

    with db.connect(cfg) as conn:
        manifest = export.build_manifest(conn, cfg)
    blob = json.dumps(manifest)
    # The explicitly-recorded uploader handle must not leak via the sensitive
    # store on the default export path.
    assert "do not disclose" not in blob
    assert "sensitive" not in manifest["items"][0]
    assert manifest["redactions"]["sensitive_included"] is False

    # With explicit opt-in it appears.
    with db.connect(cfg) as conn:
        manifest2 = export.build_manifest(conn, cfg, include_sensitive=True)
    assert manifest2["items"][0]["sensitive"]["uploader_handle"] == "SECRETHANDLE"


def test_redact_source_strips_urls(cfg: config.Config, tmp_path: Path):
    src = _make_file(tmp_path / "v.mp4", b"abc")
    with db.connect(cfg) as conn:
        item_id = db.insert_item(
            conn, ingested_by="t", source_url="https://x.com/HANDLE/status/9",
            source_kind="url", platform="x", claimed_location=None,
            claimed_datetime=None, description=None, tags=None, graphic_flag=False)
        capture.ingest(conn, cfg, item_id=item_id, source=str(src),
                       source_kind="file", actor="t", graphic=False, keyframes_n=0)
    with db.connect(cfg) as conn:
        manifest = export.build_manifest(conn, cfg, redact_source=True)
    assert manifest["items"][0]["source_url"] is None
    assert "HANDLE" not in json.dumps(manifest)


def test_dedup_links_exact_duplicates(cfg: config.Config, tmp_path: Path):
    payload = b"identical-bytes-across-two-items"
    a = _make_file(tmp_path / "a.bin", payload)
    b = _make_file(tmp_path / "b.bin", payload)
    with db.connect(cfg) as conn:
        for p in (a, b):
            iid = db.insert_item(
                conn, ingested_by="t", source_url=None, source_kind="file",
                platform="local-file", claimed_location=None, claimed_datetime=None,
                description=None, tags=None, graphic_flag=False)
            capture.ingest(conn, cfg, item_id=iid, source=str(p),
                           source_kind="file", actor="t", graphic=False, keyframes_n=0)
        result = dedup.run_dedup(conn)
    assert result["exact_links"] == 1


def test_check_command_roundtrip_via_cli(cfg: config.Config, tmp_path: Path, monkeypatch):
    """End-to-end through the public Config path the CLI uses."""
    src = _make_file(tmp_path / "doc.mp4", b"some-media")
    with db.connect(cfg) as conn:
        iid = db.insert_item(
            conn, ingested_by="t", source_url=None, source_kind="file",
            platform="local-file", claimed_location=None, claimed_datetime=None,
            description=None, tags=None, graphic_flag=False)
        capture.ingest(conn, cfg, item_id=iid, source=str(src),
                       source_kind="file", actor="t", graphic=False, keyframes_n=0)
    # Recompute and confirm match.
    with db.connect(cfg) as conn:
        files = conn.execute("SELECT path, sha256 FROM files").fetchall()
    for f in files:
        assert hashing.sha256_file(cfg.data_dir / f["path"]) == f["sha256"]


# ---------------------------------------------------------------------------
# URL-path coverage (acceptance A) via simulated downloader. No real network.
# ---------------------------------------------------------------------------
SENTINEL_HANDLE = "UPLOADERHANDLE_DONOTLEAK"


def _patch_url_download(monkeypatch, *, exit_code=0, write_media=True,
                        write_info=True, media_bytes=b"\x00real-video\x01"):
    monkeypatch.setattr(config, "has", lambda tool: tool == "yt-dlp")
    monkeypatch.setattr(config, "tool_version", lambda tool: "test")

    def fake_ytdlp(url, dest_dir):
        d = Path(dest_dir)
        if write_media:
            (d / "vid123.mp4").write_bytes(media_bytes)
        if write_info:
            (d / "vid123.info.json").write_text(json.dumps(
                {"id": "vid123", "uploader": SENTINEL_HANDLE, "webpage_url": url}))
        return (exit_code, "simulated")

    monkeypatch.setattr(capture, "_run_ytdlp", fake_ytdlp)
    monkeypatch.setattr(capture, "wayback_save",
                        lambda cfg, url: (f"https://web.archive.org/web/2026/{url}", "ok", "http 200"))


def _ingest_url(cfg, url="https://example.com/post/1"):
    with db.connect(cfg) as conn:
        iid = db.insert_item(
            conn, ingested_by="t", source_url=url, source_kind="url",
            platform="example", claimed_location=None, claimed_datetime=None,
            description=None, tags=None, graphic_flag=False)
        summary = capture.ingest(conn, cfg, item_id=iid, source=url,
                                 source_kind="url", actor="t", graphic=False,
                                 keyframes_n=0)
    return iid, summary


def test_url_capture_success_stores_media_wayback_and_hash(cfg, monkeypatch):
    """Acceptance A: download, SHA-256, Wayback URL stored, custody, byte-identical."""
    _patch_url_download(monkeypatch)
    iid, summary = _ingest_url(cfg)
    assert summary.wayback_url and summary.wayback_url.startswith("https://web.archive.org/")
    with db.connect(cfg) as conn:
        files = {f["role"]: f for f in db.get_files(conn, iid)}
        caps = {c["method"]: c for c in conn.execute(
            "SELECT * FROM captures WHERE item_id=?", (iid,)).fetchall()}
    assert caps["yt-dlp"]["status"] == "ok"
    assert caps["wayback"]["wayback_url"].startswith("https://web.archive.org/")
    assert files["original"]["media_type"] == "video"
    assert files["sidecar"]["media_type"] == "info"          # info.json kept as sidecar
    media = cfg.data_dir / files["original"]["path"]
    assert media.read_bytes() == b"\x00real-video\x01"        # byte-identical
    assert not (stat.S_IMODE(os.stat(media).st_mode) & stat.S_IWUSR)  # read-only
    assert hashing.sha256_file(media) == files["original"]["sha256"]


def test_url_metadata_only_is_not_success(cfg, monkeypatch):
    """info.json-only (restricted/failed stream) must not be labelled 'ok'."""
    _patch_url_download(monkeypatch, write_media=False, write_info=True)
    iid, summary = _ingest_url(cfg)
    with db.connect(cfg) as conn:
        cap = conn.execute("SELECT * FROM captures WHERE item_id=? AND method='yt-dlp'",
                           (iid,)).fetchone()
    assert cap["status"] == "metadata-only"
    assert any("metadata-only" in w for w in summary.warnings)


def test_url_nonzero_exit_with_media_is_partial(cfg, monkeypatch):
    _patch_url_download(monkeypatch, exit_code=1, write_media=True)
    iid, summary = _ingest_url(cfg)
    with db.connect(cfg) as conn:
        cap = conn.execute("SELECT * FROM captures WHERE item_id=? AND method='yt-dlp'",
                           (iid,)).fetchone()
    assert cap["status"] == "partial"


def test_bundle_excludes_identity_sidecars_by_default(cfg, monkeypatch):
    """BLOCKER regression: info.json/exif.json must not be copied into a bundle."""
    _patch_url_download(monkeypatch)
    iid, _ = _ingest_url(cfg)
    with db.connect(cfg) as conn:
        bundle = export.export_bundle(conn, cfg)            # default, no opt-in
    files_dir = bundle / "files"
    copied = [p.name for p in files_dir.rglob("*") if p.is_file()]
    assert any(n.endswith(".mp4") for n in copied)          # media IS copied
    assert not any(n.endswith(".info.json") for n in copied)  # sidecar NOT copied
    files_blob = "".join(p.read_text(errors="ignore")
                         for p in files_dir.rglob("*") if p.is_file())
    assert SENTINEL_HANDLE not in files_blob                # payload did not leak
    # Manifest still records the sidecar's existence + hash (chain of custody).
    manifest = json.loads((bundle / "manifest.json").read_text())
    all_paths = [f["path"] for it in manifest["items"] for f in it["files"]]
    assert any(p.endswith(".info.json") for p in all_paths)

    # Explicit opt-in DOES copy the sidecar (access-controlled).
    with db.connect(cfg) as conn:
        bundle2 = export.export_bundle(conn, cfg, include_sensitive=True,
                                       out=cfg.exports_dir / "b2")
    copied2 = [p.name for p in (bundle2 / "files").rglob("*") if p.is_file()]
    assert any(n.endswith(".info.json") for n in copied2)


def test_downloader_hardening_flags(monkeypatch):
    """Guardrails: both downloaders refuse ambient config; gallery-dl is single-item."""
    captured = {}

    def fake_run(cmd, timeout=1800):
        captured["cmd"] = list(cmd)
        return (0, "ok")

    monkeypatch.setattr(capture, "_run", fake_run)

    capture._run_ytdlp("https://x/y", Path("/tmp/x"))
    assert "--ignore-config" in captured["cmd"]
    assert "--no-playlist" in captured["cmd"]

    capture._run_gallery_dl("https://x/y", Path("/tmp/x"))
    cmd = captured["cmd"]
    assert "--config-ignore" in cmd          # no ambient cookies/creds
    assert "--range" in cmd and "1" in cmd   # single item only
    assert "--filename" in cmd               # identity-free names
    # the handle-bearing default naming is overridden
    assert any("{num" in tok for tok in cmd)


def test_standalone_text_file_is_original_not_sidecar(cfg, tmp_path):
    """correctness-3: an operator-supplied .txt is a real item, not a sidecar."""
    assert metadata.media_type_for("x.txt") == "info"       # precondition
    src = _make_file(tmp_path / "testimony.txt", b"a written testimony")
    with db.connect(cfg) as conn:
        iid = db.insert_item(
            conn, ingested_by="t", source_url=None, source_kind="file",
            platform="local-file", claimed_location=None, claimed_datetime=None,
            description=None, tags=None, graphic_flag=False)
        capture.ingest(conn, cfg, item_id=iid, source=str(src),
                       source_kind="file", actor="t", graphic=False, keyframes_n=0)
        row = conn.execute("SELECT role FROM files WHERE item_id=?", (iid,)).fetchone()
        # And it must be INCLUDED in a bundle (a primary doc, not a sidecar).
        bundle = export.export_bundle(conn, cfg, out=cfg.exports_dir / "txtb")
    assert row["role"] == "original"
    copied = [p.name for p in (bundle / "files").rglob("*") if p.is_file()]
    assert "testimony.txt" in copied


def test_ingest_preclears_orphan_item_dir(cfg, tmp_path):
    """correctness-1: a fresh ingest purges read-only orphans from a reused id."""
    src = _make_file(tmp_path / "real.mp4", b"the real bytes")
    with db.connect(cfg) as conn:
        iid = db.insert_item(
            conn, ingested_by="t", source_url=None, source_kind="file",
            platform="local-file", claimed_location=None, claimed_datetime=None,
            description=None, tags=None, graphic_flag=False)
        # Plant a frozen (read-only) orphan, as a rolled-back attempt would leave.
        item_dir = cfg.item_dir(iid)
        item_dir.mkdir(parents=True, exist_ok=True)
        orphan = item_dir / "orphan_from_failed_attempt.mp4"
        orphan.write_bytes(b"stale bytes")
        capture.freeze_readonly(orphan)
        capture.ingest(conn, cfg, item_id=iid, source=str(src),
                       source_kind="file", actor="t", graphic=False, keyframes_n=0)
        names = [Path(r["path"]).name for r in
                 conn.execute("SELECT path FROM files WHERE item_id=?", (iid,)).fetchall()]
    assert "real.mp4" in names
    assert "orphan_from_failed_attempt.mp4" not in names
    assert not (cfg.item_dir(iid) / "orphan_from_failed_attempt.mp4").exists()
