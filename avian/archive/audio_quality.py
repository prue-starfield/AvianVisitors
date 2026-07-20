#!/usr/bin/env python3
"""Append versioned waveform-quality measurements to the BirdNET archive.

Measurements are derived annotations. They never alter detector claims, reviews,
or preserved audio. HTTP requests read these rows but never compute or store them.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import io
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any

try:
    from avian.archive.sync_archive import ensure_audio_quality_schema
except ModuleNotFoundError:  # direct script execution
    from sync_archive import ensure_audio_quality_schema

DEFAULT_DEST = Path("/Volumes/Crucial Data/Hermes/bird-archive")
DEFAULT_MIRROR = Path.home() / "Library/Application Support/AvianVisitorsArchive/detections.sqlite3"
QUALITY_ALGORITHM = "frame-level-percentiles-v1"
TARGET_SAMPLE_RATE = 32_000
FRAME_LENGTH = 1_024
HOP_LENGTH = 512
DB_FLOOR = -120.0
EXPECTED_LIBROSA_VERSION = "0.11.0"
EXPECTED_SOXR_VERSION = "1.1.0"
EXPECTED_SOUNDFILE_VERSION = "0.14.0"
EXPECTED_LIBSNDFILE_VERSION = "1.2.2"
EXPECTED_NUMPY_VERSION = "2.4.6"
MAX_AUDIO_BYTES = 5 * 1024 * 1024
MAX_AUDIO_DURATION_SECONDS = 60.0
MAX_NATIVE_CHANNELS = 8
MAX_DECODED_SAMPLES = 16_000_000


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def safe_audio_path(audio_root: Path, relative: str) -> Path:
    root = audio_root.resolve()
    candidate = (root / relative).resolve()
    if root not in candidate.parents:
        raise RuntimeError("archived audio path escapes its root")
    if not candidate.is_file():
        raise RuntimeError(f"archived audio is missing: {relative}")
    return candidate


def pending_rows(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    clause = "" if limit == 0 else "LIMIT ?"
    values: tuple[Any, ...] = (QUALITY_ALGORITHM,) if limit == 0 else (QUALITY_ALGORITHM, limit)
    return list(conn.execute(
        f"""SELECT d.detection_id,d.common_name,d.observed_at_local,
                   d.audio_relpath,d.audio_sha256,d.audio_bytes
            FROM detections d
            LEFT JOIN audio_quality q
              ON q.detection_id=d.detection_id AND q.algorithm_version=?
            WHERE q.detection_id IS NULL
              AND d.audio_relpath IS NOT NULL
              AND d.audio_sha256 IS NOT NULL
              AND length(d.detection_id)=64
              AND d.detection_id NOT GLOB '*[^0-9a-f]*'
            ORDER BY d.observed_at_local DESC,d.detection_id
            {clause}""",
        values,
    ))


def amplitude_dbfs(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        return DB_FLOOR
    return max(DB_FLOOR, min(0.0, 20.0 * math.log10(value)))


def measure_samples(
    audio: Any,
    sample_rate: int,
    *,
    near_full_scale_fraction: float | None = None,
) -> dict[str, float | int | str]:
    """Measure broadband frame-level contrast; this is not acoustic SNR."""
    import numpy as np

    values = np.asarray(audio, dtype=np.float32).reshape(-1)
    if values.size == 0 or not np.isfinite(values).all():
        raise RuntimeError("decoded audio is empty or non-finite")
    if sample_rate < 8_000 or sample_rate > 192_000:
        raise RuntimeError("decoded audio sample rate is out of bounds")
    if values.size < FRAME_LENGTH:
        raise RuntimeError("decoded audio is too short for one analysis frame")
    if near_full_scale_fraction is None:
        near_full_scale_fraction = float(np.mean(np.abs(values) >= 0.999))
    if not math.isfinite(near_full_scale_fraction) or not 0 <= near_full_scale_fraction <= 1:
        raise RuntimeError("near-full-scale fraction is out of bounds")

    level_values = np.clip(values, -1.0, 1.0)
    frames = np.lib.stride_tricks.sliding_window_view(
        level_values,
        FRAME_LENGTH,
    )[::HOP_LENGTH]
    if frames.shape[0] == 0:
        raise RuntimeError("audio level analysis produced no frames")
    frames64 = frames.astype(np.float64, copy=False)
    rms = np.sqrt(np.mean(frames64 * frames64, axis=1))
    frame_levels = np.clip(
        20.0 * np.log10(np.maximum(rms, 1e-6)),
        DB_FLOOR,
        0.0,
    )
    if frame_levels.size == 0 or not np.isfinite(frame_levels).all():
        raise RuntimeError("audio level analysis produced no finite frames")
    quiet_level = float(np.percentile(frame_levels, 20, method="linear"))
    high_energy_level = float(np.percentile(frame_levels, 95, method="linear"))
    contrast = max(0.0, min(120.0, high_energy_level - quiet_level))
    duration = float(len(values) / sample_rate)
    measured = (
        quiet_level, high_energy_level, contrast,
        near_full_scale_fraction, duration,
    )
    if not all(math.isfinite(value) for value in measured):
        raise RuntimeError("audio contrast analysis produced a non-finite value")
    return {
        "algorithm_version": QUALITY_ALGORITHM,
        "sample_rate": sample_rate,
        "duration_seconds": duration,
        # Legacy storage column names remain stable; public DTO labels are honest.
        "noise_floor_dbfs": quiet_level,
        "signal_level_dbfs": high_energy_level,
        "signal_contrast_db": contrast,
        "clipping_fraction": near_full_scale_fraction,
    }


def validate_decode_metadata(frame_count: int, sample_rate: int, channels: int) -> None:
    if frame_count < 1 or sample_rate < 8_000 or sample_rate > 192_000:
        raise RuntimeError("decoded native audio metadata is out of bounds")
    if channels < 1 or channels > MAX_NATIVE_CHANNELS:
        raise RuntimeError("decoded native audio channel count is out of bounds")
    if frame_count / sample_rate > MAX_AUDIO_DURATION_SECONDS:
        raise RuntimeError("decoded native audio duration exceeds analysis limit")
    if frame_count * channels > MAX_DECODED_SAMPLES:
        raise RuntimeError("decoded native audio allocation exceeds analysis limit")


def measure_audio(audio_bytes: bytes) -> dict[str, float | int | str]:
    import librosa  # type: ignore[import-not-found]
    import numpy as np
    import soundfile  # type: ignore[import-not-found]
    import soxr  # type: ignore[import-not-found]

    versions = {
        "librosa": (librosa.__version__, EXPECTED_LIBROSA_VERSION),
        "soxr": (soxr.__version__, EXPECTED_SOXR_VERSION),
        "soundfile": (soundfile.__version__, EXPECTED_SOUNDFILE_VERSION),
        "libsndfile": (soundfile.__libsndfile_version__, EXPECTED_LIBSNDFILE_VERSION),
        "numpy": (np.__version__, EXPECTED_NUMPY_VERSION),
    }
    mismatches = [
        f"{name}={actual} (expected {expected})"
        for name, (actual, expected) in versions.items()
        if actual != expected
    ]
    if mismatches:
        raise RuntimeError("audio contrast runtime version mismatch: " + ", ".join(mismatches))

    with soundfile.SoundFile(io.BytesIO(audio_bytes)) as native_audio:
        native_rate = int(native_audio.samplerate)
        frame_count = int(native_audio.frames)
        channels = int(native_audio.channels)
        validate_decode_metadata(frame_count, native_rate, channels)
        native_values = native_audio.read(
            frames=frame_count,
            dtype="float32",
            always_2d=True,
        )
    native_values = np.asarray(native_values, dtype=np.float32)
    if native_values.size == 0 or not np.isfinite(native_values).all():
        raise RuntimeError("decoded native audio is empty or non-finite")
    if native_values.ndim != 2 or native_values.shape[1] < 1:
        raise RuntimeError("decoded native audio has an unsupported channel layout")
    near_full_scale = float(np.mean(np.abs(native_values) >= 0.999))
    mono = np.mean(native_values.astype(np.float64), axis=1).astype(np.float32)
    native_rate = int(native_rate)
    if native_rate < 8_000 or native_rate > 192_000:
        raise RuntimeError("decoded native audio sample rate is out of bounds")
    if native_rate != TARGET_SAMPLE_RATE:
        mono = librosa.resample(
            mono,
            orig_sr=native_rate,
            target_sr=TARGET_SAMPLE_RATE,
            res_type="soxr_hq",
            fix=True,
            scale=False,
        )
    return measure_samples(
        mono,
        TARGET_SAMPLE_RATE,
        near_full_scale_fraction=near_full_scale,
    )


def publish_mirror(db_path: Path, mirror_path: Path) -> None:
    if __package__:
        from .sync_archive import mirror_archive_db
    else:
        from sync_archive import mirror_archive_db
    mirror_archive_db(db_path, mirror_path)


def validated_archived_audio_bytes(value: Any, detection_id: str) -> int:
    if type(value) is not int:
        raise RuntimeError(f"invalid archived audio size: {detection_id}")
    if value < 1 or value > MAX_AUDIO_BYTES:
        raise RuntimeError(f"archived audio exceeds analysis limit: {detection_id}")
    return value


def measure_archive(
    db_path: Path,
    audio_root: Path,
    mirror_path: Path | None,
    limit: int,
    verbose: bool,
) -> int:
    if not db_path.is_file():
        raise RuntimeError(f"canonical archive database is unavailable: {db_path}")
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("canonical archive failed quick_check")
        ensure_audio_quality_schema(conn)
        rows = pending_rows(conn, limit)
        prepared: list[tuple[Any, ...]] = []
        computed_at = now_iso()
        for row in rows:
            expected_bytes = validated_archived_audio_bytes(
                row["audio_bytes"], row["detection_id"],
            )
            audio_path = safe_audio_path(audio_root, row["audio_relpath"])
            if audio_path.stat().st_size != expected_bytes:
                raise RuntimeError(f"archived audio size mismatch: {row['detection_id']}")
            with audio_path.open("rb") as handle:
                audio_bytes = handle.read(expected_bytes + 1)
            if len(audio_bytes) != expected_bytes:
                raise RuntimeError(f"archived audio size mismatch: {row['detection_id']}")
            if hashlib.sha256(audio_bytes).hexdigest() != row["audio_sha256"]:
                raise RuntimeError(f"archived audio checksum mismatch: {row['detection_id']}")
            result = measure_audio(audio_bytes)
            prepared.append((
                row["detection_id"], result["algorithm_version"], result["sample_rate"],
                result["duration_seconds"], result["noise_floor_dbfs"],
                result["signal_level_dbfs"], result["signal_contrast_db"],
                result["clipping_fraction"], computed_at,
            ))
            if verbose:
                print(
                    f"{row['detection_id']} {row['common_name']}: "
                    f"contrast={result['signal_contrast_db']} dB "
                    f"floor={result['noise_floor_dbfs']} dBFS"
                )
        before = conn.total_changes
        with conn:
            conn.executemany(
                """INSERT INTO audio_quality (
                       detection_id,algorithm_version,sample_rate,duration_seconds,
                       noise_floor_dbfs,signal_level_dbfs,signal_contrast_db,
                       clipping_fraction,computed_at
                   ) VALUES (?,?,?,?,?,?,?,?,?)""",
                prepared,
            )
        measured = conn.total_changes - before
    finally:
        conn.close()
    if mirror_path is not None:
        publish_mirror(db_path, mirror_path)
    return measured


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DEST / "detections.sqlite3")
    parser.add_argument("--audio-root", type=Path, default=DEFAULT_DEST / "audio")
    parser.add_argument("--mirror", type=Path, default=DEFAULT_MIRROR)
    parser.add_argument("--limit", type=int, default=50, help="0 measures every pending clip")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.limit < 0:
        raise ValueError("--limit must be non-negative")
    lock_path = args.db.parent / ".sync.lock"
    if not lock_path.parent.is_dir():
        raise RuntimeError(f"archive volume is not mounted: {lock_path.parent}")
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        measured = measure_archive(
            args.db, args.audio_root,
            None if str(args.mirror) == "" else args.mirror,
            args.limit, args.verbose,
        )
    if args.verbose:
        print(f"measured={measured}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Audio quality analysis failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
