"""OpenTimestamps: a trustless second anchor beside RFC 3161.

RFC 3161 relies on an authority signing truthfully. OpenTimestamps (OTS)
instead commits a digest into the Bitcoin blockchain through public calendar
servers, so a completed proof is verifiable by anyone against public block
headers with no authority to trust. The two anchors fail independently.

Lifecycle. A fresh proof is PENDING: the calendars have accepted the digest
and will commit it in a future Bitcoin block (typically within hours).
``upgrade-stamps`` later fetches the Bitcoin attestation and the proof becomes
COMPLETE. Registered files are immutable here, so an upgrade never rewrites
the pending receipt: it writes the complete proof as a NEW file,
``<name>.bitcoin.ots``, beside the pending ``<name>.ots``, and both stay in
the chain of custody.

Privacy. The OTS client appends its own random nonce to the file digest
before anything reaches a calendar (the ``append <nonce> / sha256`` ops at the
head of every proof), so calendars never see a file hash.

Verification without a Bitcoin node. The reference client can only fully
verify against a local node, which this tool's users will not run. So
``verify-stamps`` does a LIGHT verification: it reads the attested block
height and merkle root from the proof itself, fetches that block from two
independent public explorers, and requires both to agree. Full trustless
verification with a node remains available to anyone holding the ``.ots``
file and the standard ``ots`` tool.

Implementation: the ``ots`` CLI (from the optional ``opentimestamps-client``
package, ``pip install -e ".[ots]"``) performs the network operations, stamp
and upgrade, exactly as the reference tool does; the ``opentimestamps``
library parses proofs to read state. Everything degrades gracefully when the
package is absent.
"""

from __future__ import annotations

import binascii
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import config, db, hashing

STAMP_METHOD = "ots"
PROOF_ROLE = "proof"
COMPLETE_SUFFIX = ".bitcoin.ots"

# Two independent public explorers sharing the Esplora API. Light verification
# requires them to agree on the attested block's merkle root.
EXPLORERS = (
    ("blockstream.info", "https://blockstream.info/api"),
    ("mempool.space", "https://mempool.space/api"),
)


# ---------------------------------------------------------------------------
# Availability and small helpers
# ---------------------------------------------------------------------------
def ots_binary() -> str | None:
    """The ots CLI ships with the same package as the library; prefer the copy
    beside the running interpreter (the venv), then PATH."""
    cand = Path(sys.executable).parent / "ots"
    if cand.exists():
        return str(cand)
    return shutil.which("ots")


def available() -> bool:
    return config.has("opentimestamps") and ots_binary() is not None


def _run_ots(args: list[str], timeout: int = 120) -> tuple[int, str]:
    exe = ots_binary()
    if not exe:
        return 127, "ots not found"
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True,
                           timeout=timeout, check=False)
        tail = (p.stdout + p.stderr).strip().splitlines()
        return p.returncode, "\n".join(tail[-6:])
    except (subprocess.SubprocessError, OSError) as e:
        return 1, str(e)


def _rel(cfg: config.Config, path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(cfg.data_dir.resolve()))
    except ValueError:
        return str(path)


def _iso(epoch) -> str | None:
    try:
        return datetime.fromtimestamp(int(epoch), timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def _paths(pending_path: Path) -> tuple[Path, Path]:
    """(stamped file, complete-proof path) for a pending ``<name>.ots``."""
    target = pending_path.with_name(pending_path.name[:-len(".ots")])
    return target, target.with_name(target.name + COMPLETE_SUFFIX)


# ---------------------------------------------------------------------------
# Proof parsing (library) and light verification (explorers)
# ---------------------------------------------------------------------------
def inspect(proof: bytes) -> dict:
    """State read from the proof itself: file digest, pending calendars, and
    any Bitcoin attestation (height plus the committed merkle root)."""
    from opentimestamps.core.notary import (BitcoinBlockHeaderAttestation,  # type: ignore
                                            PendingAttestation)
    from opentimestamps.core.serialize import BytesDeserializationContext  # type: ignore
    from opentimestamps.core.timestamp import DetachedTimestampFile  # type: ignore

    dtf = DetachedTimestampFile.deserialize(BytesDeserializationContext(proof))
    calendars, bitcoin = [], None
    for msg, att in dtf.timestamp.all_attestations():
        if isinstance(att, PendingAttestation):
            calendars.append(att.uri)
        elif isinstance(att, BitcoinBlockHeaderAttestation) and bitcoin is None:
            # OTS keeps the merkle root in internal byte order; explorers show it reversed.
            bitcoin = {"height": att.height,
                       "merkle_root": binascii.hexlify(msg[::-1]).decode()}
    return {"file_digest": binascii.hexlify(dtf.file_digest).decode(),
            "calendars": sorted(set(calendars)), "bitcoin": bitcoin,
            "state": "complete" if bitcoin else "pending"}


def _fetch_block(base: str, height: int, timeout: int = 20) -> dict:
    import requests  # type: ignore
    bh = requests.get(f"{base}/block-height/{height}", timeout=timeout).text.strip()
    return requests.get(f"{base}/block/{bh}", timeout=timeout).json()


def light_verify(bitcoin: dict) -> dict:
    """Compare the proof's committed merkle root with the attested block as
    reported by two independent explorers. No Bitcoin node needed."""
    agree, disagree, unreachable, block_time = [], [], [], None
    for name, base in EXPLORERS:
        try:
            blk = _fetch_block(base, bitcoin["height"])
        except Exception:
            unreachable.append(name)
            continue
        if blk.get("merkle_root") == bitcoin["merkle_root"]:
            agree.append(name)
            if block_time is None:
                block_time = _iso(blk.get("timestamp"))
        else:
            disagree.append(name)
    return {"ok": bool(agree) and not disagree, "agree": agree,
            "disagree": disagree, "unreachable": unreachable, "block_time": block_time}


# ---------------------------------------------------------------------------
# Registration and custody helpers
# ---------------------------------------------------------------------------
def _register_proof(conn, cfg: config.Config, item_id: int, p: Path) -> None:
    db.add_file_row(conn, item_id=item_id, rel_path=_rel(cfg, p), media_type="proof",
                    role=PROOF_ROLE, sha256=hashing.sha256_file(p), phash=None,
                    bytes_=p.stat().st_size, original_filename=p.name)


def _skip(conn, item_id, actor, rel, why) -> dict:
    db.log_custody(conn, item_id=item_id, actor=actor, action="ots_skipped",
                   detail={"file": rel, "reason": why})
    return {"method": STAMP_METHOD, "status": "skipped", "state": None,
            "file": rel, "gen_time": None, "detail": why}


def _fail(conn, item_id, actor, rel, why) -> dict:
    db.add_capture_row(conn, item_id=item_id, method=STAMP_METHOD, tool="ots",
                       status="failed", detail=json.dumps({"file": rel, "error": why}))
    db.log_custody(conn, item_id=item_id, actor=actor, action="ots_failed",
                   detail={"file": rel, "reason": why})
    return {"method": STAMP_METHOD, "status": "failed", "state": None,
            "file": rel, "gen_time": None, "detail": why}


# ---------------------------------------------------------------------------
# Stamping at ingest
# ---------------------------------------------------------------------------
def stamp_file(conn, cfg: config.Config, *, item_id: int, path: Path,
               sha256_hex: str, actor: str) -> dict:
    """Submit one stored file to the OTS calendars and register the pending
    proof. Never raises: absence of the client or a network failure is
    recorded, not fatal."""
    rel = _rel(cfg, path)
    if not cfg.ots_enabled:
        return _skip(conn, item_id, actor, rel, "opentimestamps disabled in config")
    if not available():
        return _skip(conn, item_id, actor, rel, "opentimestamps-client not installed")
    proof_path = path.with_name(path.name + ".ots")
    if proof_path.exists():
        return _skip(conn, item_id, actor, rel, "proof already exists")

    code, detail = _run_ots(["stamp", str(path)])
    if not proof_path.exists():
        return _fail(conn, item_id, actor, rel, f"ots stamp failed (exit {code}): {detail}")
    try:
        info = inspect(proof_path.read_bytes())
    except Exception as e:
        return _fail(conn, item_id, actor, rel, f"unreadable proof: {e}")
    if info["file_digest"] != sha256_hex:
        return _fail(conn, item_id, actor, rel, "proof digest does not match the file")

    config._chmod_quiet(proof_path, config.ORIGINAL_MODE)
    _register_proof(conn, cfg, item_id, proof_path)
    db.add_capture_row(conn, item_id=item_id, method=STAMP_METHOD, tool="ots",
                       tool_version=config.tool_version("opentimestamps"),
                       status="pending",
                       detail=json.dumps({"file": rel, "state": "pending",
                                          "calendars": info["calendars"],
                                          "proof": _rel(cfg, proof_path)}))
    db.log_custody(conn, item_id=item_id, actor=actor, action="ots_pending",
                   detail={"file": rel, "calendars": info["calendars"],
                           "proof": _rel(cfg, proof_path)})
    return {"method": STAMP_METHOD, "status": "ok", "state": "pending", "file": rel,
            "gen_time": None, "detail": f"{len(info['calendars'])} calendars"}


# ---------------------------------------------------------------------------
# Upgrade (pending -> complete) and re-verification
# ---------------------------------------------------------------------------
def list_pending_proofs(conn, item_id: int | None = None) -> list:
    """The pending receipts, one per stamped file (complete proofs are siblings)."""
    q = ("SELECT * FROM files WHERE role = ? AND path LIKE '%.ots' "
         "AND path NOT LIKE '%" + COMPLETE_SUFFIX + "'")
    params: list = [PROOF_ROLE]
    if item_id is not None:
        q += " AND item_id = ?"
        params.append(item_id)
    return conn.execute(q + " ORDER BY item_id, id", params).fetchall()


def upgrade_proof(conn, cfg: config.Config, *, item_id: int, pending_path: Path,
                  actor: str) -> dict:
    """Try to complete a pending proof. The pending file is never touched: a
    completed proof is written as a NEW registered file beside it."""
    rel = _rel(cfg, pending_path)
    target, complete_path = _paths(pending_path)
    if complete_path.exists():
        return {"file": rel, "state": "complete", "changed": False,
                "detail": "already complete"}
    if not available():
        return {"file": rel, "state": "pending", "changed": False,
                "detail": "opentimestamps-client not installed"}

    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / pending_path.name
        shutil.copy2(pending_path, work)
        work.chmod(0o600)                       # the copy may be rewritten; the original may not
        _, detail = _run_ots(["upgrade", str(work)])
        try:
            info = inspect(work.read_bytes())
        except Exception as e:
            return {"file": rel, "state": "pending", "changed": False,
                    "detail": f"unreadable after upgrade: {e}"}
        if info["state"] != "complete":
            db.log_custody(conn, item_id=item_id, actor=actor,
                           action="ots_upgrade_pending", detail={"proof": rel})
            return {"file": rel, "state": "pending", "changed": False,
                    "detail": "not yet confirmed in Bitcoin"}
        complete_path.write_bytes(work.read_bytes())

    config._chmod_quiet(complete_path, config.ORIGINAL_MODE)
    _register_proof(conn, cfg, item_id, complete_path)
    block = info["bitcoin"]
    block_time = None
    try:
        block_time = light_verify(block).get("block_time")   # best effort; verify-stamps is the check
    except Exception:
        pass
    db.add_capture_row(conn, item_id=item_id, method=STAMP_METHOD, tool="ots",
                       tool_version=config.tool_version("opentimestamps"), status="ok",
                       detail=json.dumps({"file": _rel(cfg, target), "state": "complete",
                                          "block_height": block["height"],
                                          "block_time": block_time,
                                          "proof": _rel(cfg, complete_path)}))
    db.log_custody(conn, item_id=item_id, actor=actor, action="ots_upgraded",
                   detail={"file": _rel(cfg, target), "block_height": block["height"],
                           "pending_proof": rel, "complete_proof": _rel(cfg, complete_path)})
    return {"file": rel, "state": "complete", "changed": True,
            "detail": f"bitcoin block {block['height']}"}


def verify_proof(cfg: config.Config, pending_path: Path, *, offline: bool = False) -> dict:
    """Re-verify one OTS proof, preferring the complete one if present.

    Always checks the proof's digest against the file's CURRENT bytes (offline
    tamper detection). For a complete proof, light-verifies the attested block
    against two explorers unless ``offline``. ``status`` is one of
    ok | pending | unconfirmed | invalid; only ``invalid`` is a failure.
    """
    target, complete_path = _paths(pending_path)
    proof_path = complete_path if complete_path.exists() else pending_path
    base = {"method": STAMP_METHOD, "file": _rel(cfg, target), "proof": _rel(cfg, proof_path)}
    if not target.exists():
        return {**base, "status": "invalid", "state": "unknown", "reason": "stamped file missing"}
    if not config.has("opentimestamps"):
        return {**base, "status": "unconfirmed", "state": "unknown",
                "reason": "opentimestamps library not installed; cannot read the proof"}
    try:
        info = inspect(proof_path.read_bytes())
    except Exception as e:
        return {**base, "status": "invalid", "state": "unknown", "reason": f"unreadable proof: {e}"}
    if info["file_digest"] != hashing.sha256_file(target):
        return {**base, "status": "invalid", "state": info["state"],
                "reason": "file bytes no longer match the proof's digest (tampered or replaced)"}
    if info["state"] == "pending":
        return {**base, "status": "pending", "state": "pending", "reason": None,
                "detail": f"{len(info['calendars'])} calendar attestations; "
                          "run upgrade-stamps once Bitcoin confirms"}
    block = info["bitcoin"]
    if offline:
        return {**base, "status": "unconfirmed", "state": "complete",
                "block_height": block["height"], "reason": None,
                "detail": f"bitcoin block {block['height']} (explorer check skipped: offline)"}
    lv = light_verify(block)
    if lv["ok"]:
        return {**base, "status": "ok", "state": "complete", "block_height": block["height"],
                "block_time": lv["block_time"], "reason": None,
                "detail": f"bitcoin block {block['height']} confirmed by {', '.join(lv['agree'])}"}
    if lv["disagree"]:
        return {**base, "status": "invalid", "state": "complete", "block_height": block["height"],
                "reason": f"committed merkle root disagrees with {', '.join(lv['disagree'])}"}
    return {**base, "status": "unconfirmed", "state": "complete", "block_height": block["height"],
            "reason": "no public explorer reachable to confirm the attested block"}
