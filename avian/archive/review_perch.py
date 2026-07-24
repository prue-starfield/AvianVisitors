#!/usr/bin/env python3
"""Append independent Google Perch 2 reviews to the BirdNET evidence archive."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import io
from pathlib import Path
import sqlite3
import sys
from typing import Any

try:
    from . import corroboration
    from .sync_archive import ensure_review_interpretation_schema
except ImportError:  # Direct script execution from this directory.
    import corroboration  # type: ignore[no-redef]
    from sync_archive import ensure_review_interpretation_schema

DEFAULT_DEST = Path("/Volumes/Crucial Data/Hermes/bird-archive")
DEFAULT_MIRROR = Path.home() / "Library/Application Support/AvianVisitorsArchive/detections.sqlite3"
DEFAULT_PERCH = Path.home() / "Library/Application Support/AvianVisitorsArchive/perch/assets"
MODEL_NAME = corroboration.PERCH_MODEL_NAME
MODEL_SHA_FILE = "SHA256SUMS"
PINNED_ASSET_SHA256 = {
    "perch_v2.onnx": "bf0c8467a924cb074663970ca4a0ab1e143602121930209657d0dff5d5cefa1f",
    "labels.csv": "e4d5c0397d8fb08bf90c6b13a34810af53504faad927e472fcc567793c9de057",
}


def load_pinned_labels(asset_root: Path) -> frozenset[str]:
    """Load only the taxonomy asset, bound directly to its versioned checksum."""
    path = asset_root / "labels.csv"
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"Perch taxonomy asset is unavailable: {path}") from exc
    if hashlib.sha256(content).hexdigest() != PINNED_ASSET_SHA256["labels.csv"]:
        raise RuntimeError("Perch taxonomy checksum mismatch")
    lines = content.decode("utf-8").splitlines()
    if len(lines) != corroboration.EXPECTED_LABEL_COUNT + 1:
        raise RuntimeError("Perch taxonomy label count is invalid")
    if lines[0] != "inat2024_fsd50k" or len(set(lines[1:])) != len(lines[1:]):
        raise RuntimeError("Perch taxonomy namespace is invalid")
    return frozenset(lines[1:])


def verify_model_assets(asset_root: Path) -> tuple[bytes, bytes]:
    model = asset_root / "perch_v2.onnx"
    labels = asset_root / "labels.csv"
    manifest = asset_root / MODEL_SHA_FILE
    if not model.is_file() or not labels.is_file() or not manifest.is_file():
        raise RuntimeError(f"Perch model assets are incomplete: {asset_root}")
    expected: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) != 2 or len(parts[0]) != 64 or any(
            char not in "0123456789abcdef" for char in parts[0]
        ):
            raise RuntimeError(f"Malformed Perch checksum manifest: {manifest}")
        digest, name = parts
        if name in expected:
            raise RuntimeError(f"Malformed Perch checksum manifest: {manifest}")
        expected[name] = digest
    if expected != PINNED_ASSET_SHA256:
        raise RuntimeError("Perch checksum manifest does not match version-controlled pins")
    verified: dict[str, bytes] = {}
    for path in (model, labels):
        content = path.read_bytes()
        if PINNED_ASSET_SHA256[path.name] != hashlib.sha256(content).hexdigest():
            raise RuntimeError(f"Perch asset checksum mismatch: {path.name}")
        verified[path.name] = content
    return verified[model.name], verified[labels.name]


def classify_verdict(claim_score: float, claim_rank: int, top_score: float) -> str:
    """Conservative policy for uncalibrated, independent Perch scores."""
    if claim_rank == 1 and claim_score >= 0.25:
        return "confirmed"
    if claim_rank > 3 and claim_score < 0.10 and top_score >= 0.75:
        return "rejected"
    return "uncertain"


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
    values: tuple[Any, ...] = () if limit == 0 else (limit,)
    return list(conn.execute(
        f"""SELECT d.detection_id,d.scientific_name,d.common_name,d.confidence,
                   d.observed_at_local,d.audio_relpath,d.audio_sha256,d.audio_bytes
            FROM detections d
            LEFT JOIN reviews r USING(detection_id)
            WHERE r.detection_id IS NULL
              AND d.audio_relpath IS NOT NULL
              AND d.audio_sha256 IS NOT NULL
              AND length(d.detection_id) = 64
              AND d.detection_id NOT GLOB '*[^0-9a-f]*'
            ORDER BY d.observed_at_local DESC,d.detection_id
            {clause}""",
        values,
    ))


def load_model(asset_root: Path):
    model_bytes, labels_bytes = verify_model_assets(asset_root)
    import numpy as np  # noqa: F401 — validates the runtime before model load
    from perch_hoplite.taxonomy import namespace
    from perch_hoplite.zoo.models_onnx import PerchV2OnnxModel

    with io.StringIO(labels_bytes.decode("utf-8")) as handle:
        classes = namespace.ClassList.from_csv(handle)
    model = PerchV2OnnxModel(
        sample_rate=32000,
        window_size_s=5.0,
        hop_size_s=5.0,
        target_peak=0.25,
        input_name="inputs",
        output_map={"embedding": "embedding", "logits": "label", "frontend": "spectrogram"},
        model_path=model_bytes,
        class_list={classes.namespace: classes},
        logit_slope=0.97,
        logit_intercept=-10.0,
    )
    return model, classes


def infer_review(model, classes, audio_bytes: bytes, scientific_name: str) -> dict[str, Any]:
    import librosa
    import numpy as np
    from scipy.special import expit

    audio, _ = librosa.load(io.BytesIO(audio_bytes), sr=model.sample_rate, mono=True)
    outputs = model.embed(audio)
    logits = outputs.logits[classes.namespace]
    scores = expit(logits).max(axis=0)
    order = np.argsort(scores)[::-1]
    labels = classes.classes
    try:
        claim_index = labels.index(scientific_name)
    except ValueError:
        top = [(labels[int(index)], float(scores[int(index)])) for index in order[:3]]
        return {
            "status": "uncertain",
            "claim_score": None,
            "claim_rank": None,
            "label_count": len(labels),
            "top": top,
            "notes": f"Perch taxonomy has no exact label for {scientific_name}; no automated conclusion.",
        }
    claim_rank = int(np.where(order == claim_index)[0][0]) + 1
    claim_score = float(scores[claim_index])
    top = [(labels[int(index)], float(scores[int(index)])) for index in order[:3]]
    status = classify_verdict(claim_score, claim_rank, top[0][1])
    top_text = "; ".join(f"{name} {score:.1%}" for name, score in top)
    if status == "confirmed":
        conclusion = "Perch independently supports the BirdNET species claim."
    elif status == "rejected":
        conclusion = "Perch strongly favours another species and does not support the BirdNET claim."
    else:
        conclusion = "Perch is inconclusive or disagrees; human review remains appropriate."
    notes = (
        f"{conclusion} Claimed species rank {claim_rank} of {len(labels)} "
        f"with score {claim_score:.1%}. Perch top results: {top_text}. "
        "Scores are independent classifier outputs, not calibrated probabilities."
    )
    return {
        "status": status,
        "claim_label": scientific_name,
        "claim_score": claim_score,
        "claim_rank": claim_rank,
        "label_count": len(labels),
        "top": top,
        "notes": notes,
    }


def publish_mirror(db_path: Path, mirror_path: Path) -> None:
    """Import the mirror helper correctly as either a package or direct script."""
    if __package__:
        from .sync_archive import mirror_archive_db
    else:
        from sync_archive import mirror_archive_db
    mirror_archive_db(db_path, mirror_path)


def review_archive(
    db_path: Path,
    audio_root: Path,
    asset_root: Path,
    mirror_path: Path | None,
    limit: int,
    verbose: bool,
) -> int:
    if not db_path.is_file():
        raise RuntimeError(f"canonical archive database is unavailable: {db_path}")
    allowed_labels = load_pinned_labels(asset_root)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("canonical archive failed quick_check")
        ensure_review_interpretation_schema(conn)
        rows = pending_rows(conn, limit)
        prepared: list[tuple[Any, ...]] = []
        interpretations: list[tuple[str, corroboration.Interpretation, str]] = []
        model_and_classes = load_model(asset_root) if rows else None
        for row in rows:
            audio_path = safe_audio_path(audio_root, row["audio_relpath"])
            audio_bytes = audio_path.read_bytes()
            if len(audio_bytes) != int(row["audio_bytes"] or -1):
                raise RuntimeError(f"archived audio size mismatch: {row['detection_id']}")
            if hashlib.sha256(audio_bytes).hexdigest() != row["audio_sha256"]:
                raise RuntimeError(f"archived audio checksum mismatch: {row['detection_id']}")
            assert model_and_classes is not None
            result = infer_review(
                *model_and_classes, audio_bytes, row["scientific_name"],
            )
            reviewed_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            prepared.append((
                row["detection_id"], result["status"], "automated independent model",
                MODEL_NAME, result["claim_score"], result["notes"],
                reviewed_at,
            ))
            if result["claim_score"] is not None and result["claim_rank"] is not None:
                interpretations.append((
                    row["detection_id"],
                    corroboration.interpret_result(result, allowed_labels),
                    reviewed_at,
                ))
            if verbose:
                print(
                    f"{row['detection_id']} {row['common_name']}: {result['status']} "
                    f"score={result['claim_score']} rank={result['claim_rank']}"
                )
        with conn:
            corroboration.backfill_interpretations(
                conn,
                interpreted_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                allowed_labels=allowed_labels,
            )
            conn.executemany(
                """INSERT INTO reviews
                   (detection_id,status,reviewer,review_model,review_score,notes,reviewed_at)
                   VALUES (?,?,?,?,?,?,?)""",
                prepared,
            )
            for detection_id, interpretation, interpreted_at in interpretations:
                corroboration.insert_interpretation(
                    conn, detection_id, interpretation, interpreted_at,
                )
        reviewed = len(prepared)
    finally:
        conn.close()
    if mirror_path is not None:
        publish_mirror(db_path, mirror_path)
    return reviewed


def reclassify_archive(
    db_path: Path,
    mirror_path: Path | None,
    asset_root: Path = DEFAULT_PERCH,
) -> int:
    """Apply the current interpretation policy without rerunning inference."""
    if not db_path.is_file():
        raise RuntimeError(f"archive database is unavailable: {db_path}")
    allowed_labels = load_pinned_labels(asset_root)
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"archive database failed quick_check: {integrity}")
        ensure_review_interpretation_schema(conn)
        with conn:
            inserted = corroboration.backfill_interpretations(
                conn,
                interpreted_at=dt.datetime.now(dt.timezone.utc).isoformat(
                    timespec="seconds"
                ),
                allowed_labels=allowed_labels,
            )
    finally:
        conn.close()
    if mirror_path is not None:
        publish_mirror(db_path, mirror_path)
    return inserted


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DEST / "detections.sqlite3")
    parser.add_argument("--audio-root", type=Path, default=DEFAULT_DEST / "audio")
    parser.add_argument("--assets", type=Path, default=DEFAULT_PERCH)
    parser.add_argument("--mirror", type=Path, default=DEFAULT_MIRROR)
    parser.add_argument("--limit", type=int, default=25, help="0 reviews every pending clip")
    parser.add_argument(
        "--reclassify-only", action="store_true",
        help="apply the current interpretation policy to stored Perch reviews only",
    )
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
        mirror = None if str(args.mirror) == "" else args.mirror
        if args.reclassify_only:
            reviewed = reclassify_archive(args.db, mirror, args.assets)
        else:
            reviewed = review_archive(
                args.db, args.audio_root, args.assets,
                mirror, args.limit, args.verbose,
            )
    if args.verbose:
        label = "reclassified" if args.reclassify_only else "reviewed"
        print(f"{label}={reviewed}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Perch review failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
