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
from datetime import date
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
KNOWN_TOOLS = ("yt-dlp", "gallery-dl", "ffmpeg", "ffprobe", "exiftool", "archivebox",
               "openssl")

# RFC 3161 Time Stamping Authority. DigiCert's public responder is free, embeds
# its full chain (root included), and its root is in the Mozilla trust store, so
# proofs verify with plain openssl anywhere. Override with RESCUE_ARCHIVING_TSA_URL
# (for example https://freetsa.org/tsr, which is self-rooted).
DEFAULT_TSA_URL = "http://timestamp.digicert.com"

# yt-dlp versions are date-stamped (YYYY.MM.DD). Platforms change their delivery
# often, so a stale build is the most likely silent break in web capture; doctor
# warns once the installed build is older than this.
YTDLP_STALE_DAYS = 90


def ytdlp_age_days(version: str | None) -> int | None:
    """Days since a date-stamped yt-dlp version, or None if it cannot be parsed.

    Tolerates a nightly suffix (``2025.11.12.232914``) by reading only the
    first three fields.
    """
    if not version:
        return None
    try:
        y, m, d = (int(p) for p in version.strip().split(".")[:3])
        built = date(y, m, d)
    except (ValueError, TypeError):
        return None
    return (date.today() - built).days


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
    # RFC 3161 timestamps: on by default, like Wayback. The TSA only ever sees
    # a nonced commitment, never a file hash. RESCUE_ARCHIVING_TIMESTAMP=0 off.
    timestamp_enabled: bool = True
    tsa_url: str = DEFAULT_TSA_URL
    tsa_timeout: int = 30
    # OpenTimestamps, the trustless second anchor: on by default, skipped
    # gracefully unless the optional [ots] extra is installed. RESCUE_ARCHIVING_OTS=0 off.
    ots_enabled: bool = True

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
    timestamp = os.environ.get("RESCUE_ARCHIVING_TIMESTAMP", "1").lower() not in ("0", "false", "no")
    tsa_url = os.environ.get("RESCUE_ARCHIVING_TSA_URL") or DEFAULT_TSA_URL
    ots = os.environ.get("RESCUE_ARCHIVING_OTS", "1").lower() not in ("0", "false", "no")
    return Config(
        root=root,
        data_dir=data_dir,
        exports_dir=exports_dir,
        operator=operator,
        wayback_enabled=wayback,
        archivebox_enabled=archivebox,
        timestamp_enabled=timestamp,
        tsa_url=tsa_url,
        ots_enabled=ots,
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
    # exiftool uses -ver, openssl uses the 'version' subcommand; the rest take --version.
    if name == "exiftool":
        args = [exe, "-ver"]
    elif name == "openssl":
        args = [exe, "version"]
    else:
        args = [exe, "--version"]
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
    caps["opentimestamps"] = Capability("opentimestamps", *_pylib("opentimestamps"))
    return caps


def _pylib(module: str) -> tuple[str | None, str | None]:
    try:
        mod = __import__(module)
    except Exception:
        return (None, None)
    version = getattr(mod, "__version__", None)
    if not version:
        # Some libraries only declare their version in package metadata.
        try:
            from importlib.metadata import version as _dist_version
            version = _dist_version(module)
        except Exception:
            version = "unknown"
    return (module, version)


def has(tool: str) -> bool:
    return capabilities().get(tool, Capability(tool, None, None)).available


def tool_version(tool: str) -> str | None:
    cap = capabilities().get(tool)
    return cap.version if cap else None
