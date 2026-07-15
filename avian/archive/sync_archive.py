#!/usr/bin/env python3
"""Append-only BirdNET detection and evidence archive.

The BirdNET Pi remains the live acquisition system. This tool snapshots its
SQLite database safely, imports immutable source rows into a separate archive,
and mirrors detection MP3s before BirdNET's disk-pressure purge can delete them.
Review and contextual scores live in separate tables so later interpretation
never mutates the original detector claim.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import Dict, Iterable, Optional, Sequence, Tuple

DETECTION_COLUMNS = (
    "Date", "Time", "Sci_Name", "Com_Name", "Confidence", "Lat", "Lon",
    "Cutoff", "Week", "Sens", "Overlap", "File_Name",
)

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS detections (
    detection_id TEXT PRIMARY KEY,
    source_rowid INTEGER,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    observed_at_local TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'America/New_York',
    scientific_name TEXT NOT NULL,
    common_name TEXT NOT NULL,
    confidence REAL NOT NULL,
    latitude REAL,
    longitude REAL,
    cutoff REAL,
    week INTEGER,
    sensitivity REAL,
    overlap REAL,
    file_name TEXT NOT NULL,
    source_model TEXT NOT NULL,
    source_host TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    audio_relpath TEXT,
    audio_sha256 TEXT,
    audio_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS detections_observed_idx
    ON detections(date, time);
CREATE INDEX IF NOT EXISTS detections_species_idx
    ON detections(scientific_name, date);
CREATE INDEX IF NOT EXISTS detections_common_idx
    ON detections(common_name, date);

CREATE TABLE IF NOT EXISTS reviews (
    detection_id TEXT PRIMARY KEY REFERENCES detections(detection_id),
    status TEXT NOT NULL CHECK(status IN
        ('pending', 'confirmed', 'rejected', 'uncertain')),
    reviewer TEXT,
    review_model TEXT,
    review_score REAL,
    notes TEXT,
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS context_scores (
    detection_id TEXT PRIMARY KEY REFERENCES detections(detection_id),
    range_model TEXT,
    occurrence_prior REAL,
    local_prior REAL,
    repetition_count INTEGER,
    posterior REAL,
    algorithm_version TEXT NOT NULL,
    computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    source_rows INTEGER,
    inserted_rows INTEGER,
    indexed_clips INTEGER,
    snapshot_sha256 TEXT,
    status TEXT NOT NULL,
    error TEXT
);

CREATE VIEW IF NOT EXISTS daily_species AS
SELECT date, scientific_name, common_name,
       count(*) AS detections,
       min(time) AS first_heard,
       max(time) AS last_heard,
       max(confidence) AS best_confidence
FROM detections
GROUP BY date, scientific_name, common_name;

CREATE VIEW IF NOT EXISTS monthly_species AS
SELECT substr(date, 1, 7) AS month, scientific_name, common_name,
       count(*) AS detections,
       count(DISTINCT date) AS days_heard,
       min(date) AS first_date,
       max(date) AS last_date,
       max(confidence) AS best_confidence
FROM detections
GROUP BY substr(date, 1, 7), scientific_name, common_name;

CREATE VIEW IF NOT EXISTS seasonality_by_month AS
SELECT substr(date, 6, 2) AS calendar_month, scientific_name, common_name,
       count(*) AS detections,
       count(DISTINCT date) AS days_heard,
       count(DISTINCT substr(date, 1, 4)) AS years_observed
FROM detections
GROUP BY substr(date, 6, 2), scientific_name, common_name;

CREATE VIEW IF NOT EXISTS review_queue AS
SELECT d.*
FROM detections d
LEFT JOIN reviews r USING(detection_id)
WHERE d.confidence < 0.90 AND r.detection_id IS NULL
ORDER BY d.confidence ASC, d.date DESC, d.time DESC;
"""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def detection_id(values: Sequence[object]) -> str:
    # Hash the immutable source payload, not the Pi rowid. Re-importing the same
    # snapshot is idempotent even after SQLite VACUUM or a database restore.
    payload = json.dumps(list(values), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def initialise_archive(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def import_snapshot(
    snapshot: Path,
    archive: Path,
    source_model: str,
    source_host: str,
    timezone: str = "America/New_York",
) -> Tuple[int, int]:
    """Import a BirdNET SQLite snapshot, returning (source_rows, inserted)."""
    src = sqlite3.connect(str(snapshot))
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(str(archive))
    try:
        dst.execute("PRAGMA synchronous=FULL")
        initialise_archive(dst)
        columns = ",".join(DETECTION_COLUMNS)
        rows = src.execute(f"SELECT rowid AS source_rowid,{columns} FROM detections")
        source_rows = inserted = 0
        ingested = now_iso()
        with dst:
            for row in rows:
                source_rows += 1
                values = tuple(row[c] for c in DETECTION_COLUMNS)
                did = detection_id(values)
                observed = f"{row['Date']}T{row['Time']}"
                cur = dst.execute(
                    """INSERT OR IGNORE INTO detections (
                        detection_id, source_rowid, date, time, observed_at_local,
                        timezone, scientific_name, common_name, confidence,
                        latitude, longitude, cutoff, week, sensitivity, overlap,
                        file_name, source_model, source_host, ingested_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        did, row["source_rowid"], row["Date"], row["Time"], observed,
                        timezone, row["Sci_Name"], row["Com_Name"], row["Confidence"],
                        row["Lat"], row["Lon"], row["Cutoff"], row["Week"],
                        row["Sens"], row["Overlap"], row["File_Name"], source_model,
                        source_host, ingested,
                    ),
                )
                inserted += int(cur.rowcount == 1)
        return source_rows, inserted
    finally:
        src.close()
        dst.close()


def index_audio(archive: Path, audio_root: Path) -> int:
    """Attach content hashes/relative paths to imported detections once copied."""
    conn = sqlite3.connect(str(archive))
    conn.row_factory = sqlite3.Row
    try:
        initialise_archive(conn)
        wanted = list(conn.execute(
            "SELECT detection_id,file_name FROM detections WHERE audio_sha256 IS NULL"
        ))
        if not wanted or not audio_root.exists():
            return 0
        by_name: Dict[str, Path] = {}
        for p in audio_root.rglob("*.mp3"):
            by_name.setdefault(p.name, p)
        indexed = 0
        with conn:
            for row in wanted:
                p = by_name.get(row["file_name"])
                if p is None:
                    continue
                rel = p.relative_to(audio_root.parent).as_posix()
                conn.execute(
                    """UPDATE detections
                       SET audio_relpath=?, audio_sha256=?, audio_bytes=?
                       WHERE detection_id=? AND audio_sha256 IS NULL""",
                    (rel, sha256_file(p), p.stat().st_size, row["detection_id"]),
                )
                indexed += 1
        return indexed
    finally:
        conn.close()


def mirror_archive_db(archive: Path, mirror: Path) -> str:
    """Atomically copy the archive onto a second physical filesystem."""
    mirror.parent.mkdir(parents=True, exist_ok=True)
    tmp = mirror.with_name(mirror.name + ".partial")
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(str(archive))
    dst = sqlite3.connect(str(tmp))
    try:
        src.backup(dst)
        result = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"mirror integrity check failed: {result}")
    finally:
        dst.close()
        src.close()
    digest = sha256_file(tmp)
    tmp.replace(mirror)
    mirror.with_suffix(mirror.suffix + ".sha256").write_text(
        f"{digest}  {mirror.name}\n"
    )
    return digest


def ssh_options(args: argparse.Namespace) -> list[str]:
    opts = [
        "-q",
        "-i", str(args.ssh_key),
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=12",
        "-o", f"UserKnownHostsFile={args.known_hosts}",
    ]
    if args.jump_host:
        opts += ["-o", f"ProxyJump={args.jump_host}"]
    return opts


def fetch_snapshot(args: argparse.Namespace, staging: Path) -> Path:
    remote_tmp = f"/tmp/birds-archive-{os.getpid()}.db"
    remote_db = shlex.quote(args.remote_db)
    remote_tmp_q = shlex.quote(remote_tmp)
    opts = ssh_options(args)
    snapshot = staging / "birds-snapshot.db"
    make = ["ssh", *opts, args.remote_host,
            f"sqlite3 {remote_db} \".backup '{remote_tmp}'\""]
    cleanup = ["ssh", *opts, args.remote_host, f"rm -f {remote_tmp_q}"]
    try:
        subprocess.run(make, check=True, timeout=60,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        subprocess.run(
            ["scp", "-q", *opts, f"{args.remote_host}:{remote_tmp}", str(snapshot)],
            check=True, timeout=60,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    finally:
        subprocess.run(cleanup, check=False, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Force SQLite to validate every page before accepting the snapshot.
    conn = sqlite3.connect(str(snapshot))
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"source snapshot integrity check failed: {result}")
    finally:
        conn.close()
    return snapshot


def mirror_audio(args: argparse.Namespace, audio_root: Path) -> None:
    audio_root.mkdir(parents=True, exist_ok=True)
    ssh_cmd = ["ssh", *ssh_options(args)]
    remote = f"{args.remote_host}:{args.remote_audio.rstrip('/')}/"
    subprocess.run(
        [
            "rsync", "-a", "--ignore-existing", "--partial",
            "-e", shlex.join(ssh_cmd), remote, str(audio_root) + "/",
        ],
        check=True,
        timeout=1800,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def record_run(
    archive: Path,
    started: str,
    status: str,
    source_rows: int = 0,
    inserted: int = 0,
    indexed: int = 0,
    snapshot_hash: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    conn = sqlite3.connect(str(archive))
    try:
        initialise_archive(conn)
        with conn:
            conn.execute(
                """INSERT INTO sync_runs
                   (started_at,completed_at,source_rows,inserted_rows,indexed_clips,
                    snapshot_sha256,status,error)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (started, now_iso(), source_rows, inserted, indexed,
                 snapshot_hash, status, error),
            )
    finally:
        conn.close()


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    home = Path.home()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dest", type=Path,
                    default=Path("/Volumes/Crucial Data/Hermes/bird-archive"))
    ap.add_argument(
        "--db-mirror", type=Path,
        default=home / "Library/Application Support/AvianVisitorsArchive/detections.sqlite3",
        help="Atomic second-device copy of the archive database",
    )
    ap.add_argument("--remote-host", default="birdnet@192.168.36.9")
    ap.add_argument("--jump-host", default="birdpic@192.168.36.36")
    ap.add_argument("--ssh-key", type=Path, default=home / ".ssh/id_ed25519")
    ap.add_argument("--known-hosts", type=Path,
                    default=Path("/tmp/birdnet-jump-kh"))
    ap.add_argument("--remote-db",
                    default="/home/birdnet/BirdNET-Pi/scripts/birds.db")
    ap.add_argument("--remote-audio",
                    default="/home/birdnet/BirdSongs/Extracted/By_Date")
    ap.add_argument("--source-model", default="BirdNET_GLOBAL_6K_V2.4_Model_FP16")
    ap.add_argument("--timezone", default="America/New_York")
    ap.add_argument("--skip-audio", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    return ap.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    args.dest = args.dest.expanduser().resolve()
    if not args.dest.parent.exists():
        raise RuntimeError(f"archive volume is not mounted: {args.dest.parent}")
    args.dest.mkdir(parents=True, exist_ok=True)
    archive = args.dest / "detections.sqlite3"
    audio_root = args.dest / "audio" / "By_Date"
    lock_path = args.dest / ".sync.lock"
    started = now_iso()

    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0  # another healthy sync is still running

        source_rows = inserted = indexed = 0
        snapshot_hash: Optional[str] = None
        try:
            with tempfile.TemporaryDirectory(prefix="bird-archive-") as td:
                snapshot = fetch_snapshot(args, Path(td))
                snapshot_hash = sha256_file(snapshot)
                source_rows, inserted = import_snapshot(
                    snapshot, archive, args.source_model,
                    args.remote_host, args.timezone,
                )
            if not args.skip_audio:
                mirror_audio(args, audio_root)
                indexed = index_audio(archive, audio_root)
            record_run(archive, started, "ok", source_rows, inserted,
                       indexed, snapshot_hash)
            mirror_path = args.db_mirror.expanduser().resolve()
            mirror_archive_db(archive, mirror_path)
            if args.verbose:
                conn = sqlite3.connect(str(archive))
                try:
                    total = conn.execute("SELECT count(*) FROM detections").fetchone()[0]
                    clips = conn.execute(
                        "SELECT count(*) FROM detections WHERE audio_sha256 IS NOT NULL"
                    ).fetchone()[0]
                finally:
                    conn.close()
                print(
                    f"archive ok source={source_rows} inserted={inserted} "
                    f"indexed={indexed} total={total} clips={clips} "
                    f"mirror={mirror_path}"
                )
            return 0
        except Exception as exc:
            if archive.exists():
                record_run(archive, started, "error", source_rows, inserted,
                           indexed, snapshot_hash, repr(exc))
            raise


if __name__ == "__main__":
    sys.exit(main())
