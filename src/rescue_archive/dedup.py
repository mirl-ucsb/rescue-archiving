"""Deduplication: detect exact and near-duplicate media across items.

Per the guardrails, dedup *links* matches; it never deletes. Exact matches
come from identical SHA-256. Near matches come from pHash Hamming distance
within a threshold (catches re-encodes, recompression, minor crops). Links are
stored once per pair with a canonical ``item_id < other_id`` ordering.
"""

from __future__ import annotations

from itertools import combinations

from . import db, hashing


def run_dedup(conn, threshold: int = hashing.DEFAULT_PHASH_THRESHOLD) -> dict:
    rows = conn.execute(
        """SELECT id, item_id, path, sha256, phash, media_type, role
           FROM files
           WHERE role IN ('original', 'keyframe')"""
    ).fetchall()

    exact_links = _link_exact(conn, rows)
    near_links = _link_near(conn, rows, threshold)
    return {
        "files_scanned": len(rows),
        "exact_links": exact_links,
        "near_links": near_links,
        "threshold": threshold,
    }


def _ordered(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _link_exact(conn, rows) -> int:
    by_sha: dict[str, set[int]] = {}
    for r in rows:
        by_sha.setdefault(r["sha256"], set()).add(r["item_id"])
    created = 0
    for sha, items in by_sha.items():
        if len(items) < 2:
            continue
        for a, b in combinations(sorted(items), 2):
            lo, hi = _ordered(a, b)
            if db.add_link(conn, item_id=lo, other_id=hi, relation="exact",
                           distance=0, detail=f"identical sha256 {sha[:16]}..."):
                db.log_custody(conn, item_id=lo, actor="dedup",
                               action="link_exact",
                               detail={"with": hi, "sha256": sha})
                created += 1
    return created


def _link_near(conn, rows, threshold: int) -> int:
    # Only compare files that carry a pHash and belong to different items.
    hashed = [r for r in rows if r["phash"]]
    created = 0
    best: dict[tuple[int, int], int] = {}
    for r1, r2 in combinations(hashed, 2):
        if r1["item_id"] == r2["item_id"]:
            continue
        dist = hashing.hamming_distance(r1["phash"], r2["phash"])
        if dist is None or dist > threshold or dist == 0:
            # dist == 0 with identical bytes is already an exact link; a 0
            # pHash distance on differing bytes still counts as near, so only
            # skip when sha matches.
            if dist == 0 and r1["sha256"] == r2["sha256"]:
                continue
            if dist is None or dist > threshold:
                continue
        lo, hi = _ordered(r1["item_id"], r2["item_id"])
        prev = best.get((lo, hi))
        if prev is None or dist < prev:
            best[(lo, hi)] = dist

    for (lo, hi), dist in best.items():
        if db.add_link(conn, item_id=lo, other_id=hi, relation="near",
                       distance=dist, detail=f"pHash hamming distance {dist}"):
            db.log_custody(conn, item_id=lo, actor="dedup", action="link_near",
                           detail={"with": hi, "distance": dist})
            created += 1
    return created


def links_for(conn, item_id: int) -> list:
    return conn.execute(
        """SELECT * FROM item_links
           WHERE item_id = ? OR other_id = ?
           ORDER BY relation, distance""",
        (item_id, item_id),
    ).fetchall()
