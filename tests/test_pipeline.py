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

from rescue_archiving import (capture, config, db, dedup, export, hashing, metadata,
                              ots, timestamp)


@pytest.fixture()
def cfg(tmp_path: Path) -> config.Config:
    c = config.Config(
        root=tmp_path,
        data_dir=tmp_path / "data",
        exports_dir=tmp_path / "exports",
        operator="tester",
        wayback_enabled=False,  # never hit the network in tests
        archivebox_enabled=False,
        timestamp_enabled=False,  # RFC 3161 needs a TSA; stamp tests enable it with mocks
        ots_enabled=False,        # OpenTimestamps needs calendars; likewise mocked
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


# ---------------------------------------------------------------------------
# Source-protection: fail-safe bundle (regression for the 2026-06 audit).
# A source URL on a handle-based platform IS the uploader's identity, and a
# bundle is the artifact that leaves the access-controlled boundary, so a
# default bundle must redact source URLs AND staff identity (operator/analyst).
# ---------------------------------------------------------------------------
def _seed_identifying_item(cfg, *, handle="URLHANDLE_CANARY",
                           operator="OP_CANARY", analyst="ANALYST_CANARY"):
    """One URL item whose source_url + wayback_url carry a handle, plus
    operator and analyst identity, mirroring a real handle-platform capture."""
    src_url = f"https://x.com/{handle}/status/9"
    with db.connect(cfg) as conn:
        iid = db.insert_item(
            conn, ingested_by=operator, source_url=src_url, source_kind="url",
            platform="x", claimed_location="Khiam", claimed_datetime=None,
            description=None, tags=None, graphic_flag=False)
        db.add_capture_row(conn, item_id=iid, method="wayback",
                           wayback_url=f"https://web.archive.org/web/2026/{src_url}",
                           status="ok")
        db.add_verification_row(conn, item_id=iid, verifier=analyst,
                                verdict="confirmed", method="geolocation", notes=None)
    return iid


def test_bundle_is_circulation_safe_by_default(cfg):
    """F1/F2 regression: a default bundle carries no source URL and no staff id."""
    _seed_identifying_item(cfg)
    with db.connect(cfg) as conn:
        bundle = export.export_bundle(conn, cfg)
    blob = (bundle / "manifest.json").read_text() + (bundle / "manifest.csv").read_text()
    for token in ("URLHANDLE_CANARY", "OP_CANARY", "ANALYST_CANARY"):
        assert token not in blob, f"{token} leaked into a default bundle"
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["redactions"]["source_urls_redacted"] is True
    assert manifest["redactions"]["identities_redacted"] is True
    assert manifest["generated_by"] is None
    assert manifest["items"][0]["ingested_by"] is None
    assert "circulation-safe" in (bundle / "README.txt").read_text()


def test_internal_bundle_retains_identifiers(cfg):
    """The --internal opt-out retains URLs + staff identity for in-boundary use."""
    _seed_identifying_item(cfg)
    with db.connect(cfg) as conn:
        bundle = export.export_bundle(conn, cfg, redact_source=False,
                                      redact_identity=False,
                                      out=cfg.exports_dir / "internal")
    blob = (bundle / "manifest.json").read_text()
    for token in ("URLHANDLE_CANARY", "OP_CANARY", "ANALYST_CANARY"):
        assert token in blob
    assert "WARNING" in (bundle / "README.txt").read_text()


def test_json_redact_source_also_strips_identity(cfg):
    """--redact-source (json path) strips staff identity as well as URLs;
    the non-identifying verdict is kept while the analyst name is dropped."""
    _seed_identifying_item(cfg)
    with db.connect(cfg) as conn:
        manifest = export.build_manifest(conn, cfg, redact_source=True,
                                         redact_identity=True)
    blob = json.dumps(manifest)
    for token in ("URLHANDLE_CANARY", "OP_CANARY", "ANALYST_CANARY"):
        assert token not in blob
    assert manifest["items"][0]["verification_status"] == "confirmed"
    assert manifest["items"][0]["verifications"][0]["verifier"] is None


# ---------------------------------------------------------------------------
# 0.2.1 hardening (from the beta run).
# ---------------------------------------------------------------------------
def test_ytdlp_staleness_helper():
    """Date-stamped yt-dlp versions yield an age in days; junk yields None."""
    from datetime import date, timedelta
    today = date.today()
    assert config.ytdlp_age_days(today.strftime("%Y.%m.%d")) in (0, 1)
    old = (today - timedelta(days=200)).strftime("%Y.%m.%d")
    assert config.ytdlp_age_days(old) in (200, 201)
    assert config.ytdlp_age_days("2025.11.12.232914") is not None  # nightly suffix
    assert config.ytdlp_age_days(None) is None
    assert config.ytdlp_age_days("not-a-version") is None
    assert config.ytdlp_age_days("2025.13") is None                 # too few / invalid


def test_include_sensitive_respects_identity_redaction(cfg):
    """recorded_by is staff identity and must honour redact_identity even inside
    the deliberately disclosed sensitive block; the disclosure itself still works."""
    with db.connect(cfg) as conn:
        iid = db.insert_item(
            conn, ingested_by="OP_CANARY", source_url=None, source_kind="file",
            platform="local-file", claimed_location=None, claimed_datetime=None,
            description=None, tags=None, graphic_flag=False)
        db.set_sensitive(conn, item_id=iid, uploader_handle="FLAGGED_HANDLE",
                         contributor_note=None, recorded_by="OP_CANARY")
        manifest = export.build_manifest(conn, cfg, include_sensitive=True,
                                         redact_identity=True)
    sens = manifest["items"][0]["sensitive"]
    assert sens["uploader_handle"] == "FLAGGED_HANDLE"   # disclosure still works
    assert sens["recorded_by"] is None                    # staff identity redacted
    assert "OP_CANARY" not in json.dumps(manifest)


# ---------------------------------------------------------------------------
# RFC 3161 timestamps (0.3.0). Network-free: openssl and the TSA are stood in.
# ---------------------------------------------------------------------------
FAKE_CHAIN = "-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----\n"
ATTESTED = "2026-09-10T17:18:04+00:00"


def _mock_tsa(monkeypatch, *, post_ok=True):
    """Stand in for openssl and the network so the stamping flow runs offline."""
    monkeypatch.setattr(config, "has", lambda tool: tool == "openssl")
    monkeypatch.setattr(config, "tool_version", lambda tool: "LibreSSL test")
    monkeypatch.setattr(timestamp, "build_query", lambda comm: b"QUERY")
    monkeypatch.setattr(timestamp, "request_stamp",
                        lambda cfg, tsq: (b"TOKEN", "ok", "http 200") if post_ok
                        else (None, "failed", "ConnectionError: offline"))
    monkeypatch.setattr(timestamp, "reply_info", lambda tsr: {
        "granted": True, "gen_time": ATTESTED,
        "policy": "2.16.840.1.114412.7.1", "raw_status": "Status: Granted."})
    monkeypatch.setattr(timestamp, "extract_chain", lambda tsr: FAKE_CHAIN)


def _stamp_cfg(cfg):
    return dataclasses.replace(cfg, timestamp_enabled=True, tsa_url="http://tsa.test")


def _ingest_file(cfg, src):
    with db.connect(cfg) as conn:
        iid = db.insert_item(
            conn, ingested_by="t", source_url=None, source_kind="file",
            platform="local-file", claimed_location=None, claimed_datetime=None,
            description=None, tags=None, graphic_flag=False)
        summary = capture.ingest(conn, cfg, item_id=iid, source=str(src),
                                 source_kind="file", actor="t", graphic=False,
                                 keyframes_n=0)
    return iid, summary


def test_commitment_hides_file_hash():
    import hashlib
    sha = hashlib.sha256(b"x").hexdigest()
    n1, n2 = b"\x01" * 32, b"\x02" * 32
    c1 = timestamp.commitment(sha, n1)
    assert c1 != sha and len(c1) == 64
    assert timestamp.commitment(sha, n1) == c1     # deterministic
    assert timestamp.commitment(sha, n2) != c1     # the nonce changes it


def test_parse_gen_time_formats():
    p = timestamp._parse_gen_time
    assert p("Sep 10 17:18:04 2026 GMT") == ATTESTED
    assert p("Sep  1 07:08:09 2026 GMT") == "2026-09-01T07:08:09+00:00"   # padded day
    assert p("Sep 10 17:18:04.5 2026 GMT") == ATTESTED                   # fractional
    assert p("garbage") is None


def test_reply_info_parses_openssl_reply(monkeypatch):
    text = ("Status info:\nStatus: Granted.\nStatus description: unspecified\n"
            "Policy OID: 2.16.840.1.114412.7.1\nTime stamp: Sep 10 17:18:04 2026 GMT\n")
    monkeypatch.setattr(timestamp, "_run",
                        lambda args, stdin=None, timeout=60: (0, text.encode(), ""))
    info = timestamp.reply_info(b"TOKEN")
    assert info["granted"] is True
    assert info["gen_time"] == ATTESTED
    assert info["policy"] == "2.16.840.1.114412.7.1"


def test_stamp_registers_proofs_and_bundle_carries_them(cfg, tmp_path, monkeypatch):
    _mock_tsa(monkeypatch)
    scfg = _stamp_cfg(cfg)
    iid, summary = _ingest_file(scfg, _make_file(tmp_path / "clip.mp4", b"evidence"))
    with db.connect(scfg) as conn:
        files = db.get_files(conn, iid)
        caps = conn.execute("SELECT * FROM captures WHERE item_id=? AND method='rfc3161'",
                            (iid,)).fetchall()
        log = [r["action"] for r in conn.execute(
            "SELECT action FROM custody_log WHERE item_id=?", (iid,)).fetchall()]
    assert summary.stamps and summary.stamps[0]["status"] == "ok"
    proofs = [f for f in files if f["role"] == "proof"]
    assert {Path(f["path"]).name for f in proofs} == {"clip.mp4.tsr", "clip.mp4.stamp.json"}
    for f in proofs:  # frozen like an original
        mode = stat.S_IMODE(os.stat(scfg.data_dir / f["path"]).st_mode)
        assert not (mode & stat.S_IWUSR)
    assert len(caps) == 1 and caps[0]["status"] == "ok" and ATTESTED in caps[0]["detail"]
    assert "timestamp_confirmed" in log
    # Proofs carry no identity, so they travel in the DEFAULT bundle, and the
    # manifest surfaces the attested time for whoever verifies it.
    with db.connect(scfg) as conn:
        bundle = export.export_bundle(conn, scfg)
    copied = {p.name for p in (bundle / "files").rglob("*") if p.is_file()}
    assert {"clip.mp4.tsr", "clip.mp4.stamp.json"} <= copied
    manifest = json.loads((bundle / "manifest.json").read_text())
    stamps = [c for c in manifest["items"][0]["captures"] if c["method"] == "rfc3161"]
    assert stamps and stamps[0]["attested_ts"] == ATTESTED


def test_timestamp_disabled_is_skipped_and_logged(cfg, tmp_path):
    iid, summary = _ingest_file(cfg, _make_file(tmp_path / "doc.txt", b"x"))  # fixture: off
    with db.connect(cfg) as conn:
        proofs = conn.execute("SELECT COUNT(*) FROM files WHERE item_id=? AND role='proof'",
                              (iid,)).fetchone()[0]
        log = [r["action"] for r in conn.execute(
            "SELECT action FROM custody_log WHERE item_id=?", (iid,)).fetchall()]
    assert proofs == 0
    assert "timestamp_skipped" in log
    assert summary.stamps[0]["status"] == "skipped"
    assert not any("timestamp" in w for w in summary.warnings)   # deliberate, not a warning


def test_stamp_failure_is_recorded_not_fatal(cfg, tmp_path, monkeypatch):
    _mock_tsa(monkeypatch, post_ok=False)
    scfg = _stamp_cfg(cfg)
    iid, summary = _ingest_file(scfg, _make_file(tmp_path / "clip.mp4", b"bytes"))
    with db.connect(scfg) as conn:
        cap = conn.execute("SELECT * FROM captures WHERE item_id=? AND method='rfc3161'",
                           (iid,)).fetchone()
        proofs = conn.execute("SELECT COUNT(*) FROM files WHERE item_id=? AND role='proof'",
                              (iid,)).fetchone()[0]
        log = [r["action"] for r in conn.execute(
            "SELECT action FROM custody_log WHERE item_id=?", (iid,)).fetchall()]
    assert summary.files                       # the ingest itself still succeeded
    assert cap["status"] == "failed" and proofs == 0
    assert "timestamp_failed" in log
    assert any("timestamp failed" in w for w in summary.warnings)


def test_verify_stamp_detects_tamper(cfg, tmp_path, monkeypatch):
    _mock_tsa(monkeypatch)
    scfg = _stamp_cfg(cfg)
    iid, _ = _ingest_file(scfg, _make_file(tmp_path / "clip.mp4", b"original evidence"))
    with db.connect(scfg) as conn:
        meta_path = scfg.data_dir / timestamp.list_stamp_metas(conn, iid)[0]["path"]
    # Signature checking is openssl's job; stand it in as OK so this test isolates
    # the commitment binding, which is ours.
    monkeypatch.setattr(timestamp, "verify", lambda tsr, comm, chain: (True, "mocked"))
    good = timestamp.verify_stamp(scfg, meta_path)
    assert good["ok"] is True and good["gen_time"] == ATTESTED
    # Alter the stamped original: the recomputed commitment can no longer match,
    # and that is caught before any signature check, fully offline.
    original = scfg.data_dir / json.loads(meta_path.read_text())["file"]
    os.chmod(original, 0o644)
    original.write_bytes(b"altered evidence")
    bad = timestamp.verify_stamp(scfg, meta_path)
    assert bad["ok"] is False and "no longer match" in bad["reason"]


# ---------------------------------------------------------------------------
# OpenTimestamps (0.4.0). Network-free: the ots CLI and the block explorers are
# stood in; proofs are synthesised with the real library so parsing is genuine.
# ---------------------------------------------------------------------------
BLOCK = 358391


def _ots_proof(digest: bytes, *, complete: bool) -> bytes:
    """A structurally valid detached proof for ``digest``: pending (calendar
    attestation) or complete (Bitcoin block attestation on the digest itself)."""
    pytest.importorskip("opentimestamps")
    from opentimestamps.core.notary import (BitcoinBlockHeaderAttestation,
                                            PendingAttestation)
    from opentimestamps.core.op import OpSHA256
    from opentimestamps.core.serialize import BytesSerializationContext
    from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp
    ts = Timestamp(digest)
    ts.attestations.add(BitcoinBlockHeaderAttestation(BLOCK) if complete
                        else PendingAttestation("https://calendar.test"))
    ctx = BytesSerializationContext()
    DetachedTimestampFile(OpSHA256(), ts).serialize(ctx)
    return ctx.getbytes()


def _fake_ots(monkeypatch, *, fail=False, upgrade_completes=False):
    """Stand in for the ots CLI: stamp writes a pending proof beside the file;
    upgrade optionally rewrites the (temp) proof as complete."""
    import hashlib
    pytest.importorskip("opentimestamps")
    monkeypatch.setattr(ots, "available", lambda: True)
    monkeypatch.setattr(config, "has", lambda tool: tool == "opentimestamps")
    monkeypatch.setattr(config, "tool_version", lambda tool: "test")

    def run(args, timeout=120):
        if fail:
            return (1, "simulated calendar failure")
        cmd, target = args[0], Path(args[1])
        if cmd == "stamp":
            digest = hashlib.sha256(target.read_bytes()).digest()
            target.with_name(target.name + ".ots").write_bytes(_ots_proof(digest, complete=False))
        elif cmd == "upgrade" and upgrade_completes:
            digest = bytes.fromhex(ots.inspect(target.read_bytes())["file_digest"])
            target.write_bytes(_ots_proof(digest, complete=True))
        return (0, "ok")
    monkeypatch.setattr(ots, "_run_ots", run)


def _fake_explorers(monkeypatch, *, agree=True, unreachable=False):
    """Stand in for the two block explorers. A complete synthetic proof commits
    the file digest itself, so the 'block' merkle root is that digest reversed."""
    import binascii

    def fetch(base, height, timeout=20):
        if unreachable:
            raise ConnectionError("offline")
        # Recover the expected root from the proof under test via a closure set by the test.
        root = fetch.expected_root if agree else "00" * 32
        return {"merkle_root": root, "timestamp": 1432827678}
    fetch.expected_root = None
    monkeypatch.setattr(ots, "_fetch_block", fetch)
    return fetch


def _ots_cfg(cfg):
    return dataclasses.replace(cfg, ots_enabled=True)


def test_ots_stamp_registers_pending_proof(cfg, tmp_path, monkeypatch):
    _fake_ots(monkeypatch)
    ocfg = _ots_cfg(cfg)
    iid, summary = _ingest_file(ocfg, _make_file(tmp_path / "clip.mp4", b"evidence"))
    with db.connect(ocfg) as conn:
        proofs = [f for f in db.get_files(conn, iid) if f["role"] == "proof"]
        cap = conn.execute("SELECT * FROM captures WHERE item_id=? AND method='ots'",
                           (iid,)).fetchone()
        log = [r["action"] for r in conn.execute(
            "SELECT action FROM custody_log WHERE item_id=?", (iid,)).fetchall()]
    res = [s for s in summary.stamps if s.get("method") == "ots"][0]
    assert res["status"] == "ok" and res["state"] == "pending"
    assert [Path(f["path"]).name for f in proofs] == ["clip.mp4.ots"]
    mode = stat.S_IMODE(os.stat(ocfg.data_dir / proofs[0]["path"]).st_mode)
    assert not (mode & stat.S_IWUSR)                    # frozen like an original
    assert cap["status"] == "pending" and "calendar.test" in cap["detail"]
    assert "ots_pending" in log


def test_ots_upgrade_writes_complete_proof_without_touching_pending(cfg, tmp_path, monkeypatch):
    _fake_ots(monkeypatch, upgrade_completes=True)
    _fake_explorers(monkeypatch, unreachable=True)     # block time is best-effort only
    ocfg = _ots_cfg(cfg)
    iid, _ = _ingest_file(ocfg, _make_file(tmp_path / "clip.mp4", b"evidence"))
    with db.connect(ocfg) as conn:
        pending = ocfg.data_dir / ots.list_pending_proofs(conn, iid)[0]["path"]
        before = hashing.sha256_file(pending)
        res = ots.upgrade_proof(conn, ocfg, item_id=iid, pending_path=pending, actor="t")
        proofs = sorted(Path(f["path"]).name for f in db.get_files(conn, iid)
                        if f["role"] == "proof")
        caps = conn.execute("SELECT status FROM captures WHERE item_id=? AND method='ots' "
                            "ORDER BY id", (iid,)).fetchall()
        log = [r["action"] for r in conn.execute(
            "SELECT action FROM custody_log WHERE item_id=?", (iid,)).fetchall()]
    assert res["changed"] is True and res["state"] == "complete"
    assert proofs == ["clip.mp4.bitcoin.ots", "clip.mp4.ots"]   # new file, pending kept
    assert hashing.sha256_file(pending) == before                # pending untouched
    assert [c["status"] for c in caps] == ["pending", "ok"]
    assert "ots_upgraded" in log
    # Running again is a no-op.
    with db.connect(ocfg) as conn:
        again = ots.upgrade_proof(conn, ocfg, item_id=iid, pending_path=pending, actor="t")
    assert again["changed"] is False and again["state"] == "complete"


def test_ots_verify_states_and_tamper(cfg, tmp_path, monkeypatch):
    import binascii, hashlib
    _fake_ots(monkeypatch, upgrade_completes=True)
    fetch = _fake_explorers(monkeypatch, agree=True)
    ocfg = _ots_cfg(cfg)
    payload = b"original evidence"
    iid, _ = _ingest_file(ocfg, _make_file(tmp_path / "clip.mp4", payload))
    with db.connect(ocfg) as conn:
        pending = ocfg.data_dir / ots.list_pending_proofs(conn, iid)[0]["path"]
    # 1. Pending: verified as far as it can be, not a failure.
    assert ots.verify_proof(ocfg, pending)["status"] == "pending"
    # 2. Complete + both explorers agree -> ok.
    with db.connect(ocfg) as conn:
        ots.upgrade_proof(conn, ocfg, item_id=iid, pending_path=pending, actor="t")
    fetch.expected_root = binascii.hexlify(hashlib.sha256(payload).digest()[::-1]).decode()
    good = ots.verify_proof(ocfg, pending)
    assert good["status"] == "ok" and good["block_height"] == BLOCK
    assert "blockstream.info" in good["detail"] and "mempool.space" in good["detail"]
    # 3. --offline never contacts explorers and reports unconfirmed.
    assert ots.verify_proof(ocfg, pending, offline=True)["status"] == "unconfirmed"
    # 4. An explorer disagreeing on the merkle root is invalid.
    _fake_explorers(monkeypatch, agree=False)
    assert ots.verify_proof(ocfg, pending)["status"] == "invalid"
    # 5. Tampered bytes fail offline, before any explorer is consulted.
    original = ocfg.data_dir / good["file"]
    os.chmod(original, 0o644)
    original.write_bytes(b"altered evidence")
    bad = ots.verify_proof(ocfg, pending, offline=True)
    assert bad["status"] == "invalid" and "no longer match" in bad["reason"]


def test_ots_skipped_when_client_absent(cfg, tmp_path, monkeypatch):
    pytest.importorskip("opentimestamps")
    monkeypatch.setattr(ots, "available", lambda: False)
    ocfg = _ots_cfg(cfg)
    iid, summary = _ingest_file(ocfg, _make_file(tmp_path / "doc.txt", b"x"))
    with db.connect(ocfg) as conn:
        proofs = conn.execute("SELECT COUNT(*) FROM files WHERE item_id=? AND role='proof'",
                              (iid,)).fetchone()[0]
        log = [r["action"] for r in conn.execute(
            "SELECT action FROM custody_log WHERE item_id=?", (iid,)).fetchall()]
    res = [s for s in summary.stamps if s.get("method") == "ots"][0]
    assert res["status"] == "skipped" and proofs == 0 and "ots_skipped" in log


def test_ots_failure_is_recorded_not_fatal(cfg, tmp_path, monkeypatch):
    _fake_ots(monkeypatch, fail=True)
    ocfg = _ots_cfg(cfg)
    iid, summary = _ingest_file(ocfg, _make_file(tmp_path / "clip.mp4", b"bytes"))
    with db.connect(ocfg) as conn:
        cap = conn.execute("SELECT status FROM captures WHERE item_id=? AND method='ots'",
                           (iid,)).fetchone()
        log = [r["action"] for r in conn.execute(
            "SELECT action FROM custody_log WHERE item_id=?", (iid,)).fetchall()]
    assert summary.files                                   # ingest still succeeded
    assert cap["status"] == "failed" and "ots_failed" in log
    assert any("opentimestamps failed" in w for w in summary.warnings)


# ---------------------------------------------------------------------------
# 0.5.0 capture robustness: classified whole-post capture and the Wayback
# fallback. Network-free: downloaders, the classifier, and archive.org are
# stood in.
# ---------------------------------------------------------------------------
def test_single_post_allow_list_is_conservative():
    assert capture.is_single_post(("twitter", "tweet"))
    assert capture.is_single_post(("instagram", "post"))
    assert capture.is_single_post(("reddit", "submission"))
    assert capture.is_single_post(("imgur", "album"))
    assert capture.is_single_post(("wikimediacommons", "file"))
    assert not capture.is_single_post(("twitter", "user"))
    assert not capture.is_single_post(("twitter", "media"))          # a user's media timeline
    assert not capture.is_single_post(("instagram", "posts"))        # plural is a feed
    assert not capture.is_single_post(("wikimediacommons", "category"))
    assert not capture.is_single_post(None)


def test_classify_url_uses_gallery_dl_matcher_offline():
    pytest.importorskip("gallery_dl")
    c = capture.classify_url
    assert c("https://x.com/USER/status/12345") == ("twitter", "tweet")
    assert c("https://x.com/USER") == ("twitter", "user")
    assert c("https://www.instagram.com/p/abcdefg/") == ("instagram", "post")
    assert c("https://www.reddit.com/r/SUB/comments/id/") == ("reddit", "submission")
    assert c("https://commons.wikimedia.org/wiki/Category:X") == ("wikimediacommons", "category")
    assert c("not a url") is None


def _patch_gallery_dl(monkeypatch, n_files=3):
    """No yt-dlp media, so ingest falls through to gallery-dl; record its call."""
    seen = {}
    monkeypatch.setattr(config, "has", lambda tool: tool in ("yt-dlp", "gallery-dl"))
    monkeypatch.setattr(config, "tool_version", lambda tool: "test")
    monkeypatch.setattr(capture, "_run_ytdlp", lambda url, d: (1, "no media"))

    def fake(url, dest_dir, whole_post=False, cap=capture.MULTI_FILE_CAP):
        seen["whole_post"], seen["cap"] = whole_post, cap
        for i in range(n_files if whole_post else 1):
            (Path(dest_dir) / f"{i + 1:04d}.jpg").write_bytes(b"\xff\xd8img" + bytes([i]))
        return (0, "ok")
    monkeypatch.setattr(capture, "_run_gallery_dl", fake)
    monkeypatch.setattr(capture, "wayback_save", lambda cfg, url: (None, "skipped", "test"))
    return seen


def _ingest_url_item(cfg, url, **kw):
    with db.connect(cfg) as conn:
        iid = db.insert_item(
            conn, ingested_by="t", source_url=url, source_kind="url", platform="x",
            claimed_location=None, claimed_datetime=None, description=None,
            tags=None, graphic_flag=False)
        summary = capture.ingest(conn, cfg, item_id=iid, source=url, source_kind="url",
                                 actor="t", graphic=False, keyframes_n=0, **kw)
        n = conn.execute("SELECT COUNT(*) FROM files WHERE item_id=? AND role='original'",
                         (iid,)).fetchone()[0]
        dl = conn.execute("SELECT detail FROM custody_log WHERE item_id=? AND action='download'",
                          (iid,)).fetchone()["detail"]
    return iid, summary, n, json.loads(dl)


def test_single_post_is_taken_whole_and_capped(cfg, monkeypatch):
    seen = _patch_gallery_dl(monkeypatch, n_files=3)
    monkeypatch.setattr(capture, "classify_url", lambda url: ("twitter", "tweet"))
    _, summary, n, dl = _ingest_url_item(cfg, "https://x.com/USER/status/1")
    assert seen["whole_post"] is True and seen["cap"] == cfg.multi_file_cap
    assert n == 3 and summary.capture_mode == "whole-post"
    assert dl["classification"] == "twitter.tweet" and dl["mode"] == "whole-post"
    assert dl["override"] is False


def test_feed_link_stays_at_one_item_and_says_why(cfg, monkeypatch):
    seen = _patch_gallery_dl(monkeypatch, n_files=3)
    monkeypatch.setattr(capture, "classify_url", lambda url: ("twitter", "user"))
    _, summary, n, dl = _ingest_url_item(cfg, "https://x.com/USER")
    assert seen["whole_post"] is False and n == 1
    assert dl["classification"] == "twitter.user" and dl["mode"] == "single-item"
    assert any("twitter.user" in w and "--whole-post" in w for w in summary.warnings)


def test_whole_post_override_is_logged_and_still_capped(cfg, monkeypatch):
    seen = _patch_gallery_dl(monkeypatch, n_files=3)
    monkeypatch.setattr(capture, "classify_url", lambda url: None)   # unclassifiable
    _, summary, n, dl = _ingest_url_item(cfg, "https://example.com/post/1", whole_post=True)
    assert seen["whole_post"] is True and seen["cap"] == cfg.multi_file_cap and n == 3
    assert dl["override"] is True and dl["mode"] == "whole-post"


def test_reaching_the_cap_is_flagged(cfg, monkeypatch):
    small = dataclasses.replace(cfg, multi_file_cap=3)
    _patch_gallery_dl(monkeypatch, n_files=3)
    monkeypatch.setattr(capture, "classify_url", lambda url: ("imgur", "album"))
    iid, summary, n, _ = _ingest_url_item(small, "https://imgur.com/a/abcde")
    with db.connect(small) as conn:
        log = [r["action"] for r in conn.execute(
            "SELECT action FROM custody_log WHERE item_id=?", (iid,)).fetchall()]
    assert n == 3 and "multi_file_flag" in log
    assert any("3-file cap" in w for w in summary.warnings)


def test_wayback_retries_transient_failures_then_succeeds(cfg, monkeypatch):
    monkeypatch.setattr(capture, "WAYBACK_RETRY_DELAYS", (0, 0, 0))
    calls = []

    class R:
        def __init__(self, code, loc=None):
            self.status_code = code
            self.headers = {"Content-Location": loc} if loc else {}
            self.url = "https://web.archive.org/save/x"

    def get(url, **kw):
        calls.append(url)
        return R(429) if len(calls) < 3 else R(200, "/web/2026/https://example.com/")
    monkeypatch.setattr("requests.get", get)
    wb, status, _ = capture.wayback_save(dataclasses.replace(cfg, wayback_enabled=True),
                                         "https://example.com/")
    assert status == "ok" and wb.endswith("/web/2026/https://example.com/")
    assert len(calls) == 3


def test_wayback_failure_falls_back_to_an_existing_snapshot(cfg, monkeypatch):
    monkeypatch.setattr(config, "has", lambda tool: False)   # no downloaders needed here
    monkeypatch.setattr(capture, "wayback_save", lambda cfg, url: (None, "failed", "http 503"))
    monkeypatch.setattr(capture, "wayback_existing", lambda cfg, url: (
        "http://web.archive.org/web/20250101000000/https://example.com/", "20250101000000"))
    wcfg = dataclasses.replace(cfg, wayback_enabled=True)
    with db.connect(wcfg) as conn:
        iid = db.insert_item(conn, ingested_by="t", source_url="https://example.com/",
                             source_kind="url", platform="example", claimed_location=None,
                             claimed_datetime=None, description=None, tags=None,
                             graphic_flag=False)
        summary = capture.ingest(conn, wcfg, item_id=iid, source="https://example.com/",
                                 source_kind="url", actor="t", graphic=False, keyframes_n=0)
        cap = conn.execute("SELECT * FROM captures WHERE item_id=? AND method='wayback'",
                           (iid,)).fetchone()
        log = [r["action"] for r in conn.execute(
            "SELECT action FROM custody_log WHERE item_id=?", (iid,)).fetchall()]
    assert cap["status"] == "existing" and "20250101" in cap["wayback_url"]
    assert "wayback_existing" in log and summary.wayback_url == cap["wayback_url"]
    assert any("earlier snapshot" in w for w in summary.warnings)


def test_retry_snapshots_adds_a_new_record_and_keeps_history(cfg, monkeypatch):
    monkeypatch.setattr(config, "has", lambda tool: False)
    monkeypatch.setattr(capture, "wayback_save", lambda cfg, url: (None, "failed", "http 503"))
    monkeypatch.setattr(capture, "wayback_existing", lambda cfg, url: (None, None))
    wcfg = dataclasses.replace(cfg, wayback_enabled=True)
    with db.connect(wcfg) as conn:
        iid = db.insert_item(conn, ingested_by="t", source_url="https://example.com/",
                             source_kind="url", platform="example", claimed_location=None,
                             claimed_datetime=None, description=None, tags=None,
                             graphic_flag=False)
        capture.ingest(conn, wcfg, item_id=iid, source="https://example.com/",
                       source_kind="url", actor="t", graphic=False, keyframes_n=0)
    # Later the archive is reachable again.
    monkeypatch.setattr(capture, "wayback_save", lambda cfg, url: (
        "https://web.archive.org/web/2026/https://example.com/", "ok", "http 200"))
    with db.connect(wcfg) as conn:
        res = capture.retry_snapshots(conn, wcfg, actor="t")
        rows = [r["status"] for r in conn.execute(
            "SELECT status FROM captures WHERE item_id=? AND method='wayback' ORDER BY id",
            (iid,)).fetchall()]
        log = [r["action"] for r in conn.execute(
            "SELECT action FROM custody_log WHERE item_id=?", (iid,)).fetchall()]
    assert res and res[0]["status"] == "ok"
    assert rows == ["failed", "ok"] and "wayback_retry" in log    # history kept, row added
    with db.connect(wcfg) as conn:
        assert capture.retry_snapshots(conn, wcfg, actor="t") == []   # nothing left to retry


def test_wayback_existing_falls_back_to_the_bare_url_form(cfg, monkeypatch):
    """Observed live: the availability API answered an HTML 429 for the
    scheme-bearing URL but returned the snapshot for the bare form."""
    class R:
        def __init__(self, payload=None):
            self._p = payload

        def json(self):
            if self._p is None:
                raise ValueError("not JSON (an HTML 429 page)")
            return self._p

    seen = []

    def get(url, params=None, timeout=None):
        seen.append(params["url"])
        if params["url"].startswith("https://"):
            return R()                                       # rate-limited HTML
        return R({"archived_snapshots": {"closest": {
            "available": True, "timestamp": "20250101000000",
            "url": "http://web.archive.org/web/20250101000000/http://example.com/"}}})
    monkeypatch.setattr("requests.get", get)
    wb, ts = capture.wayback_existing(cfg, "https://example.com/")
    assert ts == "20250101000000" and wb.endswith("/http://example.com/")
    assert seen == ["https://example.com/", "example.com"]
