"""RFC 3161 timestamping: third-party attestation that a hash existed at time T.

Why: SHA-256 plus the append-only custody log prove integrity against mistakes
and casual rewriting, but anyone with database access can drop the triggers. A
timestamp token from an independent Time Stamping Authority (TSA) makes the
record tamper-EVIDENT: the TSA signs (digest, time), and anyone can verify that
signature later with the TSA's certificate chain, without trusting this machine
or this database.

Privacy: the TSA never sees a file's hash. We stamp a nonce COMMITMENT,
``sha256(nonce || file_sha256)``, and keep the nonce locally beside the proof.
A hash of a known video is matchable; a nonced commitment is not.

Implementation: the system ``openssl`` binary builds the DER query, reads the
reply, and verifies (an optional capability, like yt-dlp); ``requests`` does the
HTTP POST. Verification tries the Mozilla roots (certifi, shipped with requests)
with the token's embedded chain as untrusted, then the embedded chain alone, so
both commercial TSAs (DigiCert) and self-rooted ones (FreeTSA) verify.

Everything needed to re-verify is stored read-only beside the original and
registered in the chain of custody with role ``proof``:
  * ``<name>.tsr``        the raw token, usable with plain ``openssl ts -verify``
  * ``<name>.stamp.json`` nonce, commitment, TSA, attested time, embedded chain
Proofs carry no identity, so they travel in every export bundle.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import config, db, hashing

PROOF_ROLE = "proof"
STAMP_METHOD = "rfc3161"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def commitment(sha256_hex: str, nonce: bytes) -> str:
    """sha256(nonce || raw file digest), hex. The TSA sees this, never the file hash."""
    return hashlib.sha256(nonce + bytes.fromhex(sha256_hex)).hexdigest()


def _parse_gen_time(s: str) -> str | None:
    """'Sep 10 17:18:04 2026 GMT' (day may be space-padded, seconds may carry a
    fraction) -> ISO-8601 UTC with second precision, or None."""
    try:
        parts = s.replace("GMT", "").split()
        mon, day, hms, year = parts[0], parts[1], parts[2].split(".")[0], parts[3]
        dt = datetime.strptime(f"{mon} {int(day)} {hms} {year}", "%b %d %H:%M:%S %Y")
        return dt.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
    except (ValueError, IndexError):
        return None


def _rel(cfg: config.Config, path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(cfg.data_dir.resolve()))
    except ValueError:
        return str(path)


def _run(args: list[str], stdin: bytes | None = None,
         timeout: int = 60) -> tuple[int, bytes, str]:
    try:
        p = subprocess.run(args, input=stdin, capture_output=True,
                           timeout=timeout, check=False)
        return p.returncode, p.stdout, (p.stderr or b"").decode(errors="replace").strip()
    except FileNotFoundError:
        return 127, b"", f"{args[0]} not found"
    except (subprocess.SubprocessError, OSError) as e:
        return 1, b"", str(e)


def _certifi_bundle() -> str | None:
    try:
        import certifi  # type: ignore
        return certifi.where()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# The RFC 3161 round trip (each step is small and separately mockable)
# ---------------------------------------------------------------------------
def build_query(commitment_hex: str) -> bytes | None:
    """DER TimeStampReq for the commitment. ``-cert`` asks the TSA to embed its
    chain so the proof can be verified later without contacting anyone."""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "q.tsq"
        code, _, _ = _run(["openssl", "ts", "-query", "-digest", commitment_hex,
                           "-sha256", "-cert", "-no_nonce", "-out", str(out)])
        if code == 0 and out.exists():
            return out.read_bytes()
    return None


def request_stamp(cfg: config.Config, tsq: bytes) -> tuple[bytes | None, str, str]:
    """POST the query to the TSA. Returns (tsr_bytes, status, detail)."""
    try:
        import requests  # type: ignore
    except Exception:
        return None, "failed", "requests library not installed"
    try:
        r = requests.post(
            cfg.tsa_url, data=tsq, timeout=cfg.tsa_timeout,
            headers={"Content-Type": "application/timestamp-query",
                     "User-Agent": "rescue-archiving (+RFC 3161 timestamp)"},
        )
        if r.status_code != 200 or not r.content:
            return None, "failed", f"http {r.status_code}"
        return r.content, "ok", f"http {r.status_code}"
    except Exception as e:  # network errors, timeouts
        return None, "failed", f"{type(e).__name__}: {e}"


def reply_info(tsr: bytes) -> dict:
    """Status and TSA-attested time, read from ``openssl ts -reply -text``."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "r.tsr"
        p.write_bytes(tsr)
        _, out, _ = _run(["openssl", "ts", "-reply", "-in", str(p), "-text"])
    text = out.decode(errors="replace")
    info: dict = {"granted": "Status: Granted" in text, "gen_time": None,
                  "policy": None, "raw_status": None}
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Status:"):
            info["raw_status"] = s
        elif s.startswith("Policy OID:"):
            info["policy"] = s.split(":", 1)[1].strip()
        elif s.startswith("Time stamp:"):
            info["gen_time"] = _parse_gen_time(s.split(":", 1)[1].strip())
    return info


def extract_chain(tsr: bytes) -> str:
    """PEM of the certificates the TSA embedded in the token (often incl. root)."""
    with tempfile.TemporaryDirectory() as td:
        p, tok, chain = Path(td) / "r.tsr", Path(td) / "tok.der", Path(td) / "chain.pem"
        p.write_bytes(tsr)
        c1, _, _ = _run(["openssl", "ts", "-reply", "-in", str(p),
                         "-token_out", "-out", str(tok)])
        if c1 != 0 or not tok.exists():
            return ""
        c2, _, _ = _run(["openssl", "pkcs7", "-inform", "DER", "-in", str(tok),
                         "-print_certs", "-out", str(chain)])
        if c2 != 0 or not chain.exists():
            return ""
        return chain.read_text()


def verify(tsr: bytes, commitment_hex: str, chain_pem: str) -> tuple[bool, str]:
    """Does the token bind this commitment? Returns (ok, trust_path).

    Tries the Mozilla roots with the embedded chain as untrusted (commercial
    TSAs), then the embedded chain alone (self-rooted TSAs such as FreeTSA).
    """
    with tempfile.TemporaryDirectory() as td:
        p, chain = Path(td) / "r.tsr", Path(td) / "chain.pem"
        p.write_bytes(tsr)
        chain.write_text(chain_pem)
        base = ["openssl", "ts", "-verify", "-digest", commitment_hex,
                "-sha256", "-in", str(p)]
        roots = _certifi_bundle()
        if roots and chain_pem:
            code, out, _ = _run(base + ["-CAfile", roots, "-untrusted", str(chain)])
            if code == 0 and b"Verification: OK" in out:
                return True, "mozilla-roots+embedded-chain"
        if chain_pem:
            code, out, _ = _run(base + ["-CAfile", str(chain)])
            if code == 0 and b"Verification: OK" in out:
                return True, "embedded-chain"
    return False, "none"


# ---------------------------------------------------------------------------
# Stamping at ingest
# ---------------------------------------------------------------------------
def _register_proof(conn, cfg: config.Config, item_id: int, p: Path) -> None:
    db.add_file_row(conn, item_id=item_id, rel_path=_rel(cfg, p), media_type="proof",
                    role=PROOF_ROLE, sha256=hashing.sha256_file(p), phash=None,
                    bytes_=p.stat().st_size, original_filename=p.name)


def _skip(conn, item_id, actor, rel, why) -> dict:
    db.log_custody(conn, item_id=item_id, actor=actor, action="timestamp_skipped",
                   detail={"file": rel, "reason": why})
    return {"status": "skipped", "file": rel, "gen_time": None, "detail": why}


def _fail(conn, item_id, actor, rel, why) -> dict:
    db.add_capture_row(conn, item_id=item_id, method=STAMP_METHOD, tool="openssl-ts",
                       status="failed", detail=json.dumps({"file": rel, "error": why}))
    db.log_custody(conn, item_id=item_id, actor=actor, action="timestamp_failed",
                   detail={"file": rel, "reason": why})
    return {"status": "failed", "file": rel, "gen_time": None, "detail": why}


def stamp_file(conn, cfg: config.Config, *, item_id: int, path: Path,
               sha256_hex: str, actor: str) -> dict:
    """Request an RFC 3161 timestamp for one stored file and register the proof.

    Returns {'status': 'ok' | 'failed' | 'skipped', 'file', 'gen_time', 'detail'}.
    Never raises: a missing tool or a network failure is recorded, not fatal,
    exactly as a failed Wayback request is.
    """
    rel = _rel(cfg, path)
    if not cfg.timestamp_enabled:
        return _skip(conn, item_id, actor, rel, "timestamps disabled in config")
    if not config.has("openssl"):
        return _skip(conn, item_id, actor, rel, "openssl binary not found")

    nonce = secrets.token_bytes(32)
    comm = commitment(sha256_hex, nonce)
    tsq = build_query(comm)
    if not tsq:
        return _fail(conn, item_id, actor, rel, "could not build timestamp query")
    tsr, status, detail = request_stamp(cfg, tsq)
    if status != "ok" or not tsr:
        return _fail(conn, item_id, actor, rel, f"TSA request failed: {detail}")
    info = reply_info(tsr)
    if not info["granted"]:
        return _fail(conn, item_id, actor, rel, f"TSA did not grant: {info['raw_status']}")
    chain = extract_chain(tsr)

    # Write the proof beside the original, freeze it, register it as evidence.
    tsr_path = path.with_name(path.name + ".tsr")
    meta_path = path.with_name(path.name + ".stamp.json")
    tsr_path.write_bytes(tsr)
    meta_path.write_text(json.dumps({
        "method": STAMP_METHOD, "file": rel, "file_sha256": sha256_hex,
        "nonce": nonce.hex(), "commitment": comm, "tsa_url": cfg.tsa_url,
        "gen_time": info["gen_time"], "policy": info["policy"],
        "token": tsr_path.name, "chain_pem": chain,
        "verify_hint": ("commitment = sha256(bytes.fromhex(nonce) + "
                        "bytes.fromhex(file_sha256)); then: openssl ts -verify "
                        "-digest <commitment> -sha256 -in <token> "
                        "-CAfile <roots> -untrusted <chain.pem>"),
    }, indent=2))
    for p in (tsr_path, meta_path):
        config._chmod_quiet(p, config.ORIGINAL_MODE)
        _register_proof(conn, cfg, item_id, p)
    db.add_capture_row(conn, item_id=item_id, method=STAMP_METHOD, tool="openssl-ts",
                       tool_version=config.tool_version("openssl"), status="ok",
                       detail=json.dumps({"file": rel, "gen_time": info["gen_time"],
                                          "tsa": cfg.tsa_url, "commitment": comm}))
    db.log_custody(conn, item_id=item_id, actor=actor, action="timestamp_confirmed",
                   detail={"file": rel, "gen_time": info["gen_time"],
                           "tsa": cfg.tsa_url, "proof": _rel(cfg, tsr_path)})
    return {"status": "ok", "file": rel, "gen_time": info["gen_time"], "detail": detail}


# ---------------------------------------------------------------------------
# Re-verification
# ---------------------------------------------------------------------------
def list_stamp_metas(conn, item_id: int | None = None) -> list[sqlite3.Row]:
    q = "SELECT * FROM files WHERE role = ? AND path LIKE '%.stamp.json'"
    params: list = [PROOF_ROLE]
    if item_id is not None:
        q += " AND item_id = ?"
        params.append(item_id)
    return conn.execute(q + " ORDER BY item_id, id", params).fetchall()


def verify_stamp(cfg: config.Config, meta_path: Path) -> dict:
    """Re-verify one proof: recompute the commitment from the file's CURRENT
    bytes and the stored nonce, then check the token binds it.

    The commitment check alone is an offline tamper detector: altered bytes
    cannot reproduce the commitment the TSA signed.
    """
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError) as e:
        return {"file": str(meta_path), "ok": False, "reason": f"unreadable proof: {e}"}
    file_path = cfg.data_dir / meta["file"]
    token_path = meta_path.with_name(meta["token"])
    if not file_path.exists():
        return {"file": meta["file"], "ok": False, "reason": "stamped file missing"}
    if not token_path.exists():
        return {"file": meta["file"], "ok": False, "reason": "token missing"}
    comm = commitment(hashing.sha256_file(file_path), bytes.fromhex(meta["nonce"]))
    if comm != meta["commitment"]:
        return {"file": meta["file"], "ok": False, "gen_time": meta.get("gen_time"),
                "reason": "file bytes no longer match the stamped commitment "
                          "(tampered or replaced)"}
    if not config.has("openssl"):
        return {"file": meta["file"], "ok": False, "gen_time": meta.get("gen_time"),
                "reason": "openssl binary not found; cannot verify the signature"}
    ok, trust_path = verify(token_path.read_bytes(), comm, meta.get("chain_pem", ""))
    return {"file": meta["file"], "ok": ok, "gen_time": meta.get("gen_time"),
            "trust_path": trust_path, "tsa": meta.get("tsa_url"),
            "reason": None if ok else "token signature or chain did not verify"}
