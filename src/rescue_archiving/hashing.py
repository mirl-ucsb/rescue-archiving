"""Integrity (SHA-256) and perceptual (pHash) hashing.

SHA-256 uses only the standard library, so exact-integrity hashing is always
available. Perceptual hashing depends on Pillow + imagehash; when those are
absent ``phash_image`` returns ``None`` and the pipeline simply records no
pHash rather than failing.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 1024 * 1024  # 1 MiB streaming reads; never load whole media into RAM.


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def phash_image(path: str | Path) -> str | None:
    """Perceptual hash of an image as a hex string, or None if unavailable.

    Returns None when Pillow/imagehash are not installed or the file is not a
    decodable image (e.g. a video container or a corrupt file).
    """
    try:
        import imagehash  # type: ignore
        from PIL import Image  # type: ignore
    except Exception:
        return None
    try:
        with Image.open(path) as img:
            return str(imagehash.phash(img))
    except Exception:
        return None


def hamming_distance(a: str | None, b: str | None) -> int | None:
    """Hamming distance between two hex pHash strings.

    Compares bit-for-bit on the integer value of equal-length hex hashes.
    Returns None if either hash is missing or lengths differ (not comparable).
    """
    if not a or not b or len(a) != len(b):
        return None
    try:
        x = int(a, 16) ^ int(b, 16)
    except ValueError:
        return None
    return x.bit_count()


# Default near-duplicate threshold for 64-bit pHashes. Conservative: small
# enough to avoid false links, large enough to catch re-encodes/recompression.
DEFAULT_PHASH_THRESHOLD = 8
