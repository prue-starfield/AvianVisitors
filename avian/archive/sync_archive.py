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
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import Dict, Optional, Sequence, Tuple

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

CREATE TABLE IF NOT EXISTS configuration_epochs (
    effective_at TEXT PRIMARY KEY,
    audio_model TEXT NOT NULL,
    range_model TEXT NOT NULL,
    occurrence_threshold REAL NOT NULL,
    confidence_threshold REAL NOT NULL,
    sensitivity REAL NOT NULL,
    overlap REAL NOT NULL,
    reason TEXT
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

AUDIO_QUALITY_TABLE_DDL = """
CREATE TABLE audio_quality (
    detection_id TEXT NOT NULL REFERENCES detections(detection_id),
    algorithm_version TEXT NOT NULL,
    sample_rate INTEGER NOT NULL CHECK(sample_rate BETWEEN 8000 AND 192000),
    duration_seconds REAL NOT NULL CHECK(duration_seconds > 0 AND duration_seconds <= 3600),
    noise_floor_dbfs REAL NOT NULL CHECK(noise_floor_dbfs BETWEEN -120 AND 0),
    signal_level_dbfs REAL NOT NULL CHECK(signal_level_dbfs BETWEEN -120 AND 0),
    signal_contrast_db REAL NOT NULL CHECK(signal_contrast_db BETWEEN 0 AND 120),
    clipping_fraction REAL NOT NULL CHECK(clipping_fraction BETWEEN 0 AND 1),
    computed_at TEXT NOT NULL,
    PRIMARY KEY (detection_id, algorithm_version)
)
"""
AUDIO_QUALITY_INDEX_DDL = """
CREATE INDEX audio_quality_algorithm_idx
    ON audio_quality(algorithm_version, signal_contrast_db DESC)
"""
AUDIO_QUALITY_TRIGGER_DDLS = {
    "audio_quality_no_update": """
        CREATE TRIGGER audio_quality_no_update
        BEFORE UPDATE ON audio_quality
        BEGIN
            SELECT RAISE(ABORT, 'audio_quality is append-only');
        END
    """,
    "audio_quality_no_delete": """
        CREATE TRIGGER audio_quality_no_delete
        BEFORE DELETE ON audio_quality
        BEGIN
            SELECT RAISE(ABORT, 'audio_quality is append-only');
        END
    """,
    "audio_quality_no_replace": """
        CREATE TRIGGER audio_quality_no_replace
        BEFORE INSERT ON audio_quality
        WHEN EXISTS (
            SELECT 1 FROM audio_quality
            WHERE detection_id=NEW.detection_id
              AND algorithm_version=NEW.algorithm_version
        )
        BEGIN
            SELECT RAISE(ABORT, 'audio_quality version already exists');
        END
    """,
    "audio_quality_no_explicit_rowid": """
        CREATE TRIGGER audio_quality_no_explicit_rowid
        BEFORE INSERT ON audio_quality
        WHEN NEW.rowid > 0
        BEGIN
            SELECT RAISE(ABORT, 'audio_quality rowid is managed internally');
        END
    """,
    "audio_quality_positive_rowid": """
        CREATE TRIGGER audio_quality_positive_rowid
        AFTER INSERT ON audio_quality
        WHEN NEW.rowid <= 0
        BEGIN
            SELECT RAISE(ABORT, 'audio_quality rowid is managed internally');
        END
    """,
}

REVIEW_INTERPRETATION_TABLE_DDL = """
CREATE TABLE review_interpretations (
    detection_id TEXT NOT NULL REFERENCES reviews(detection_id),
    policy_version TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN
        ('corroborated', 'uncorroborated', 'model_conflict')),
    claim_score REAL NOT NULL CHECK(claim_score BETWEEN 0 AND 1),
    claim_rank INTEGER NOT NULL CHECK(claim_rank > 0),
    label_count INTEGER NOT NULL CHECK(label_count >= claim_rank),
    top_label TEXT NOT NULL,
    top_score REAL NOT NULL CHECK(top_score BETWEEN 0 AND 1),
    interpreted_at TEXT NOT NULL,
    PRIMARY KEY (detection_id, policy_version)
)
"""
REVIEW_INTERPRETATION_INDEX_DDL = """
CREATE INDEX review_interpretations_policy_idx
    ON review_interpretations(policy_version, outcome, detection_id)
"""
REVIEW_TRIGGER_DDLS = {
    "reviews_no_update": """
        CREATE TRIGGER reviews_no_update
        BEFORE UPDATE ON reviews
        BEGIN
            SELECT RAISE(ABORT, 'reviews is append-only');
        END
    """,
    "reviews_no_delete": """
        CREATE TRIGGER reviews_no_delete
        BEFORE DELETE ON reviews
        BEGIN
            SELECT RAISE(ABORT, 'reviews is append-only');
        END
    """,
    "reviews_no_replace": """
        CREATE TRIGGER reviews_no_replace
        BEFORE INSERT ON reviews
        WHEN EXISTS (
            SELECT 1 FROM reviews WHERE detection_id=NEW.detection_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'reviews is append-only');
        END
    """,
}
REVIEW_INTERPRETATION_TRIGGER_DDLS = {
    "review_interpretations_no_update": """
        CREATE TRIGGER review_interpretations_no_update
        BEFORE UPDATE ON review_interpretations
        BEGIN
            SELECT RAISE(ABORT, 'review_interpretations is append-only');
        END
    """,
    "review_interpretations_no_delete": """
        CREATE TRIGGER review_interpretations_no_delete
        BEFORE DELETE ON review_interpretations
        BEGIN
            SELECT RAISE(ABORT, 'review_interpretations is append-only');
        END
    """,
    "review_interpretations_no_replace": """
        CREATE TRIGGER review_interpretations_no_replace
        BEFORE INSERT ON review_interpretations
        WHEN EXISTS (
            SELECT 1 FROM review_interpretations
            WHERE detection_id=NEW.detection_id
              AND policy_version=NEW.policy_version
        )
        BEGIN
            SELECT RAISE(ABORT, 'review_interpretations is append-only');
        END
    """,
}


def _normalise_schema_sql(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().rstrip(";")).lower()


def _schema_object_sql(
    conn: sqlite3.Connection,
    object_type: str,
    name: str,
) -> Optional[str]:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type=? AND name=?",
        (object_type, name),
    ).fetchone()
    return None if row is None else row[0]


def _attest_schema_object(
    conn: sqlite3.Connection,
    object_type: str,
    name: str,
    expected_sql: str,
) -> None:
    actual = _schema_object_sql(conn, object_type, name)
    if actual is None:
        raise RuntimeError(f"missing required archive schema object: {name}")
    if _normalise_schema_sql(actual) != _normalise_schema_sql(expected_sql):
        raise RuntimeError(f"noncanonical archive schema object: {name}")


def validate_audio_quality_schema(conn: sqlite3.Connection) -> None:
    """Fail closed unless the append-only annotation schema is exact."""
    _attest_schema_object(
        conn, "table", "audio_quality", AUDIO_QUALITY_TABLE_DDL,
    )
    _attest_schema_object(
        conn, "index", "audio_quality_algorithm_idx", AUDIO_QUALITY_INDEX_DDL,
    )
    actual_indexes = {
        row[1] for row in conn.execute("PRAGMA index_list(audio_quality)")
    }
    expected_indexes = {
        "audio_quality_algorithm_idx", "sqlite_autoindex_audio_quality_1",
    }
    if actual_indexes != expected_indexes:
        raise RuntimeError(
            "noncanonical audio_quality indexes: "
            f"expected {sorted(expected_indexes)}, found {sorted(actual_indexes)}"
        )
    actual_triggers = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='audio_quality'"
        )
    }
    expected_triggers = set(AUDIO_QUALITY_TRIGGER_DDLS)
    if actual_triggers != expected_triggers:
        raise RuntimeError("noncanonical audio_quality trigger set")
    for name, ddl in AUDIO_QUALITY_TRIGGER_DDLS.items():
        _attest_schema_object(conn, "trigger", name, ddl)


def ensure_audio_quality_schema(conn: sqlite3.Connection) -> None:
    """Atomically install or attest the append-only annotation schema."""
    if conn.in_transaction:
        raise RuntimeError("audio-quality migration requires a clean transaction boundary")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        table_sql = _schema_object_sql(conn, "table", "audio_quality")
        if table_sql is None:
            conn.execute(AUDIO_QUALITY_TABLE_DDL)
        elif _normalise_schema_sql(table_sql) != _normalise_schema_sql(
            AUDIO_QUALITY_TABLE_DDL
        ):
            raise RuntimeError("noncanonical archive schema object: audio_quality")

        objects = [
            ("index", "audio_quality_algorithm_idx", AUDIO_QUALITY_INDEX_DDL),
            *(("trigger", name, ddl) for name, ddl in AUDIO_QUALITY_TRIGGER_DDLS.items()),
        ]
        for object_type, name, ddl in objects:
            actual = _schema_object_sql(conn, object_type, name)
            if actual is None:
                conn.execute(ddl)
            elif _normalise_schema_sql(actual) != _normalise_schema_sql(ddl):
                raise RuntimeError(f"noncanonical archive schema object: {name}")
        validate_audio_quality_schema(conn)
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def validate_review_interpretation_schema(conn: sqlite3.Connection) -> None:
    """Fail closed unless the versioned corroboration schema is exact."""
    actual_review_triggers = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='reviews'"
        )
    }
    if actual_review_triggers != set(REVIEW_TRIGGER_DDLS):
        raise RuntimeError("noncanonical reviews trigger set")
    for name, ddl in REVIEW_TRIGGER_DDLS.items():
        _attest_schema_object(conn, "trigger", name, ddl)
    _attest_schema_object(
        conn, "table", "review_interpretations", REVIEW_INTERPRETATION_TABLE_DDL,
    )
    _attest_schema_object(
        conn, "index", "review_interpretations_policy_idx",
        REVIEW_INTERPRETATION_INDEX_DDL,
    )
    actual_indexes = {
        row[1] for row in conn.execute("PRAGMA index_list(review_interpretations)")
    }
    expected_indexes = {
        "review_interpretations_policy_idx",
        "sqlite_autoindex_review_interpretations_1",
    }
    if actual_indexes != expected_indexes:
        raise RuntimeError("noncanonical review_interpretations indexes")
    actual_triggers = {
        row[0] for row in conn.execute(
            """SELECT name FROM sqlite_master
               WHERE type='trigger' AND tbl_name='review_interpretations'"""
        )
    }
    if actual_triggers != set(REVIEW_INTERPRETATION_TRIGGER_DDLS):
        raise RuntimeError("noncanonical review_interpretations trigger set")
    for name, ddl in REVIEW_INTERPRETATION_TRIGGER_DDLS.items():
        _attest_schema_object(conn, "trigger", name, ddl)


def ensure_review_interpretation_schema(conn: sqlite3.Connection) -> None:
    """Atomically install or attest the append-only corroboration schema."""
    if conn.in_transaction:
        raise RuntimeError("review-interpretation migration requires a clean transaction boundary")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        objects = [
            *(("trigger", name, ddl) for name, ddl in REVIEW_TRIGGER_DDLS.items()),
            ("table", "review_interpretations", REVIEW_INTERPRETATION_TABLE_DDL),
            ("index", "review_interpretations_policy_idx", REVIEW_INTERPRETATION_INDEX_DDL),
            *(("trigger", name, ddl) for name, ddl in REVIEW_INTERPRETATION_TRIGGER_DDLS.items()),
        ]
        for object_type, name, ddl in objects:
            actual = _schema_object_sql(conn, object_type, name)
            if actual is None:
                conn.execute(ddl)
            elif _normalise_schema_sql(actual) != _normalise_schema_sql(ddl):
                raise RuntimeError(f"noncanonical archive schema object: {name}")
        validate_review_interpretation_schema(conn)
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


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
    ensure_audio_quality_schema(conn)
    ensure_review_interpretation_schema(conn)


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


def refresh_web_audio_cache(
    archive: Path,
    canonical_audio_root: Path,
    cache_root: Path,
    max_bytes: int,
) -> Tuple[int, int]:
    """Materialise the newest evidence clips into a bounded internal cache.

    The external archive remains canonical. This cache exists only because
    macOS background services cannot reliably read removable volumes without
    a broad TCC grant. New files are checksum-verified and atomically replaced;
    files outside the newest ``max_bytes`` window are removed.
    """
    canonical_audio_root = canonical_audio_root.resolve()
    cache_root = cache_root.resolve()
    home = Path.home().resolve()
    if (
        cache_root in {Path("/"), home, canonical_audio_root}
        or cache_root in canonical_audio_root.parents
        or canonical_audio_root in cache_root.parents
    ):
        raise RuntimeError(f"refusing unsafe web cache root: {cache_root}")
    cache_root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(archive))
    conn.row_factory = sqlite3.Row
    try:
        rows = list(conn.execute(
            """SELECT detection_id,audio_relpath,audio_sha256,audio_bytes
               FROM detections
               WHERE audio_relpath IS NOT NULL AND audio_sha256 IS NOT NULL
               ORDER BY date DESC,time DESC,detection_id"""
        ))
    finally:
        conn.close()

    keep: set[str] = set()
    selected: list[sqlite3.Row] = []
    selected_bytes = 0
    for row in rows:
        size = int(row["audio_bytes"] or 0)
        if size <= 0 or selected_bytes + size > max_bytes:
            continue
        rel = str(row["audio_relpath"])
        keep.add(rel)
        selected.append(row)
        selected_bytes += size

    # Remove every unselected regular file or symlink first, including stale
    # .partial files. The byte cap applies to the whole dedicated cache tree,
    # not merely files whose names happen to end in .mp3.
    for cached in cache_root.rglob("*"):
        if not (cached.is_file() or cached.is_symlink()):
            continue
        rel = cached.relative_to(cache_root).as_posix()
        if rel not in keep:
            cached.unlink()

    copied = 0
    for row in selected:
        rel = Path(str(row["audio_relpath"]))
        if rel.is_absolute() or ".." in rel.parts:
            raise RuntimeError(f"invalid archive audio path: {rel}")
        source = (canonical_audio_root / rel).resolve()
        target = (cache_root / rel).resolve()
        if canonical_audio_root not in source.parents or cache_root not in target.parents:
            raise RuntimeError(f"archive audio path escaped its root: {rel}")
        if not source.is_file():
            keep.discard(rel.as_posix())
            if target.is_file() or target.is_symlink():
                target.unlink()
            continue
        if target.is_file() and target.stat().st_size == int(row["audio_bytes"]):
            if sha256_file(target) == row["audio_sha256"]:
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".partial")
        tmp.unlink(missing_ok=True)
        shutil.copy2(source, tmp)
        if sha256_file(tmp) != row["audio_sha256"]:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"web audio cache checksum mismatch: {rel}")
        tmp.replace(target)
        copied += 1

    for directory in sorted(
        (p for p in cache_root.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts), reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    actual_bytes = sum(
        p.stat().st_size for p in cache_root.rglob("*") if p.is_file()
    )
    if actual_bytes > max_bytes:
        raise RuntimeError(
            f"web audio cache exceeded hard cap: {actual_bytes} > {max_bytes}"
        )
    return copied, actual_bytes


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
            completed = None if status == "running" else now_iso()
            existing = conn.execute(
                "SELECT run_id FROM sync_runs WHERE started_at=? ORDER BY run_id DESC LIMIT 1",
                (started,),
            ).fetchone()
            values = (completed, source_rows, inserted, indexed,
                      snapshot_hash, status, error)
            if existing:
                conn.execute(
                    """UPDATE sync_runs SET completed_at=?,source_rows=?,inserted_rows=?,
                       indexed_clips=?,snapshot_sha256=?,status=?,error=? WHERE run_id=?""",
                    values + (existing[0],),
                )
            else:
                conn.execute(
                    """INSERT INTO sync_runs
                       (started_at,completed_at,source_rows,inserted_rows,indexed_clips,
                        snapshot_sha256,status,error) VALUES (?,?,?,?,?,?,?,?)""",
                    (started,) + values,
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
    ap.add_argument(
        "--web-audio-cache", type=Path,
        default=home / "Library/Application Support/AvianVisitorsArchive/audio",
        help="Bounded internal playback cache; the external archive remains canonical",
    )
    ap.add_argument(
        "--web-audio-cache-bytes", type=int, default=2 * 1024 * 1024 * 1024,
        help="Maximum internal playback-cache size (default: 2 GiB)",
    )
    ap.add_argument("--remote-host", default="birdnet@192.168.36.9")
    ap.add_argument("--jump-host", default="birdpic@192.168.36.36")
    ap.add_argument("--ssh-key", type=Path, default=home / ".ssh/id_ed25519")
    ap.add_argument(
        "--known-hosts", type=Path,
        default=home / ".ssh/birdnet_known_hosts",
    )
    ap.add_argument("--remote-db",
                    default="/home/birdnet/BirdNET-Pi/scripts/birds.db")
    ap.add_argument("--remote-audio",
                    default="/home/birdnet/BirdSongs/Extracted/By_Date")
    ap.add_argument("--source-model", default="BirdNET_GLOBAL_6K_V2.4_Model_FP16")
    ap.add_argument("--timezone", default="America/New_York")
    ap.add_argument("--skip-audio", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    return ap.parse_args(argv)


def resolve_runtime_paths(args: argparse.Namespace) -> argparse.Namespace:
    """Resolve user-supplied paths and fail before creating archive state."""
    args.dest = args.dest.expanduser().resolve()
    args.known_hosts = args.known_hosts.expanduser().resolve()
    if not args.known_hosts.is_file():
        raise RuntimeError(f"SSH known-hosts trust file is missing: {args.known_hosts}")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = resolve_runtime_paths(parse_args(argv))
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
        record_run(archive, started, "running")
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
            mirror_path = args.db_mirror.expanduser().resolve()
            cache_copied = cache_bytes = 0
            if not args.skip_audio:
                cache_copied, cache_bytes = refresh_web_audio_cache(
                    archive,
                    args.dest / "audio",
                    args.web_audio_cache.expanduser().resolve(),
                    args.web_audio_cache_bytes,
                )
            record_run(archive, started, "ok", source_rows, inserted,
                       indexed, snapshot_hash)
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
                    f"mirror={mirror_path} web_cache_copied={cache_copied} "
                    f"web_cache_bytes={cache_bytes}"
                )
            return 0
        except Exception as exc:
            if archive.exists():
                record_run(archive, started, "error", source_rows, inserted,
                           indexed, snapshot_hash, repr(exc))
            raise


if __name__ == "__main__":
    sys.exit(main())
