"""Metadata extraction: media-type sniffing, EXIF, and video keyframes.

All functions degrade gracefully:
  * ``extract_exif`` needs the exiftool binary (+ pyexiftool); returns {} if absent.
  * ``extract_keyframes`` needs ffmpeg/ffprobe; returns [] if absent.

Keyframes are written to a working directory (config.tmp_dir by the caller),
never over the read-only originals. They are themselves hashed and registered
as files with role='keyframe' so their provenance is tracked too.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import config

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts", ".flv"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".tif", ".tiff", ".bmp"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"}


def media_type_for(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in {".json", ".txt", ".vtt", ".srt", ".description"}:
        return "info"
    if ext in {".html", ".htm", ".warc", ".warc.gz", ".mhtml"}:
        return "page"
    return "other"


def extract_exif(path: str | Path) -> dict:
    """Return EXIF/metadata dict via exiftool, or {} if unavailable.

    Note: EXIF can itself carry sensitive data (GPS, device serials, author).
    Callers must treat this payload as potentially sensitive and keep it out
    of the default export unless reviewed.
    """
    if not config.has("exiftool"):
        return {}
    try:
        import exiftool  # type: ignore
    except Exception:
        return {}
    try:
        with exiftool.ExifToolHelper() as et:  # type: ignore
            meta = et.get_metadata(str(path))
            return meta[0] if meta else {}
    except Exception:
        return {}


def _ffprobe_duration(path: str | Path) -> float | None:
    if not config.has("ffprobe"):
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", str(path)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        data = json.loads(out.stdout or "{}")
        dur = data.get("format", {}).get("duration")
        return float(dur) if dur is not None else None
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return None


def extract_keyframes(
    video_path: str | Path,
    out_dir: str | Path,
    n: int = 5,
) -> list[Path]:
    """Extract up to ``n`` evenly spaced keyframes to ``out_dir`` as PNGs.

    Returns the list of frame paths actually produced (possibly empty). Does
    not modify the source video. Frames are decoded only; the original is read
    sequentially and never rewritten.
    """
    if n <= 0 or not config.has("ffmpeg"):
        return []
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(video_path).stem
    duration = _ffprobe_duration(video_path)
    frames: list[Path] = []

    if duration and duration > 0:
        # Sample at fractional offsets so frames span the whole clip.
        for i in range(n):
            ts = duration * (i + 1) / (n + 1)
            dst = out_dir / f"{stem}_kf{i:02d}.png"
            ok = _ffmpeg_frame_at(video_path, ts, dst)
            if ok and dst.exists():
                frames.append(dst)
    else:
        # Unknown duration: fall back to ffmpeg's thumbnail filter for n frames.
        pattern = out_dir / f"{stem}_kf%02d.png"
        try:
            subprocess.run(
                ["ffmpeg", "-v", "quiet", "-i", str(video_path),
                 "-vf", f"thumbnail,fps=1", "-frames:v", str(n),
                 "-y", str(pattern)],
                capture_output=True, timeout=300, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        frames = sorted(out_dir.glob(f"{stem}_kf*.png"))
    return frames


def _ffmpeg_frame_at(video_path: str | Path, ts: float, dst: Path) -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-v", "quiet", "-ss", f"{ts:.3f}", "-i", str(video_path),
             "-frames:v", "1", "-q:v", "2", "-y", str(dst)],
            capture_output=True, timeout=120, check=False,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False
