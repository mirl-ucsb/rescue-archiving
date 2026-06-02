"""Configuration, path resolution, and external-tool capability detection.

Design notes
------------
The pipeline orchestrates several external binaries (yt-dlp, gallery-dl,
ffmpeg, exiftool, archivebox). None of them are import-time dependencies:
each is an *optional capability*. The tool must run, ingest local files,
hash, log custody, and export even when every external binary is absent.
``capabilities()`` reports what is available so the rest of the code can
degrade gracefully and the custody log can record exactly which tool (and
version) produced each artifact.

Paths are resolved once, here, so storage layout is a single source of
truth. Everything lives under a single ``data/`` root that is locked down
to the owner (0700); the SQLite database is 0600; captured originals are
frozen read-only (0444) at ingest and never rewritten.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# ---------------------------------------------------------------------------
# Filesystem permission policy (POSIX). Enforced at ingest / init time.
# ---------------------------------------------------------------------------
DIR_MODE = 0o700  # data root and subdirs: owner-only
DB_MODE = 0o600  # sqlite db: owner read/write
ORIGINAL_MODE = 0o444  # captured originals: read-only for everyone
EXPORT_DIR_MODE = 0o700

# External tools we know how to drive. Order is display order.
KNOWN_TOOLS = ("yt-dlp", "gallery-dl", "ffmpeg", "ffprobe", "exiftool", "archivebox")


def _env_path(var: str, default: Path) -> Path:
    raw = os.environ.get(var)
    return Path(raw).expanduser().resolve() if raw else default


@dataclass(frozen=True)
class Config:
    """Resolved runtime configuration.

    Override the project root with ``RESCUE_ARCHIVING_ROOT`` and the data
    directory with ``RESCUE_ARCHIVING_DATA``. The operator identity used for
    custody-log actor attribution comes from ``RESCUE_ARCHIVING_OPERATOR``
    (falling back to the OS user).
    """

    root: Path
    data_dir: Path
    exports_dir: Path
    operator: str
    wayback_enabled: bool = True
    archivebox_enabled: bool = False
    # Wayback Save API politeness / robustness.
    wayback_endpoint: str = "https://web.archive.org/save/"
    wayback_timeout: int = 120

    @property
    def db_path(self) -> Path:
        return self.data_dir / "rescue_archiving.db"

    @property
    def originals_dir(self) -> Path:
        # One subdir per item id keeps originals immutable and grouped.
        return self.data_dir / "originals"

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def tmp_dir(self) -> Path:
        # Working space for keyframe extraction etc. Never holds the
        # canonical copy; canonical originals live read-only under originals/.
        return self.data_dir / "tmp"

    def item_dir(self, item_id: int) -> Path:
        return self.originals_dir / f"item_{item_id:06d}"

    def ensure_dirs(self) -> None:
        """Create the data tree with locked-down permissions (idempotent)."""
        for d in (self.data_dir, self.originals_dir, self.snapshots_dir,
                  self.tmp_dir, self.exports_dir):
            d.mkdir(parents=True, exist_ok=True)
            _chmod_quiet(d, DIR_MODE)


def _chmod_quiet(path: Path, mode: int) -> None:
    """chmod that never raises (e.g. on filesystems without POSIX modes)."""
    try:
        os.chmod(path, mode)
    except (OSError, NotImplementedError):
        pass


@lru_cache(maxsize=1)
def get_config() -> Config:
    root = _env_path("RESCUE_ARCHIVING_ROOT", Path.cwd())
    data_dir = _env_path("RESCUE_ARCHIVING_DATA", root / "data")
    exports_dir = _env_path("RESCUE_ARCHIVING_EXPORTS", root / "exports")
    operator = (
        os.environ.get("RESCUE_ARCHIVING_OPERATOR")
        or os.environ.get("USER")
        or os.environ.get("USERNAME")
        or "unknown-operator"
    )
    wayback = os.environ.get("RESCUE_ARCHIVING_WAYBACK", "1").lower() not in ("0", "false", "no")
    archivebox = os.environ.get("RESCUE_ARCHIVING_ARCHIVEBOX", "0").lower() in ("1", "true", "yes")
    return Config(
        root=root,
        data_dir=data_dir,
        exports_dir=exports_dir,
        operator=operator,
        wayback_enabled=wayback,
        archivebox_enabled=archivebox,
    )


# ---------------------------------------------------------------------------
# Tool capability detection
# ---------------------------------------------------------------------------
@dataclass
class Capability:
    name: str
    path: str | None
    version: str | None

    @property
    def available(self) -> bool:
        return self.path is not None


def _tool_version(name: str, exe: str) -> str | None:
    # exiftool uses -ver; everything else we care about supports --version.
    args = [exe, "-ver"] if name == "exiftool" else [exe, "--version"]
    try:
        out = subprocess.run(
            args, capture_output=True, text=True, timeout=15, check=False
        )
        line = (out.stdout or out.stderr or "").strip().splitlines()
        return line[0].strip() if line else None
    except (OSError, subprocess.SubprocessError):
        return None


@lru_cache(maxsize=1)
def capabilities() -> dict[str, Capability]:
    """Detect external tools once per process. Cached."""
    caps: dict[str, Capability] = {}
    for name in KNOWN_TOOLS:
        exe = shutil.which(name)
        version = _tool_version(name, exe) if exe else None
        caps[name] = Capability(name=name, path=exe, version=version)
    # Optional Python libs (pHash, EXIF) are reported too.
    caps["imagehash"] = Capability("imagehash", *_pylib("imagehash"))
    caps["Pillow"] = Capability("Pillow", *_pylib("PIL"))
    caps["pyexiftool"] = Capability("pyexiftool", *_pylib("exiftool"))
    return caps


def _pylib(module: str) -> tuple[str | None, str | None]:
    try:
        mod = __import__(module)
    except Exception:
        return (None, None)
    return (module, getattr(mod, "__version__", "unknown"))


def has(tool: str) -> bool:
    return capabilities().get(tool, Capability(tool, None, None)).available


def tool_version(tool: str) -> str | None:
    cap = capabilities().get(tool)
    return cap.version if cap else None
