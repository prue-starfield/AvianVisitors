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

DEFAULT_DEST = Path("/Volumes/Crucial Data/Hermes/bird-archive")
DEFAULT_MIRROR = Path.home() / "Library/Application Support/AvianVisitorsArchive/detections.sqlite3"
DEFAULT_PERCH = Path.home() / "Library/Application Support/AvianVisitorsArchive/perch/assets"
MODEL_NAME = "Google Perch 2.0 ONNX (inat2024_fsd50k)"
MODEL_SHA_FILE = "SHA256SUMS"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_model_assets(asset_root: Path) -> tuple[Path, Path]:
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
    for path in (model, labels):
        actual = sha256_file(path)
        if expected.get(path.name) != actual:
            raise RuntimeError(f"Perch asset checksum mismatch: {path.name}")
    return model, labels


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
    model_path, labels_path = verify_model_assets(asset_root)
    import numpy as np  # noqa: F401 — validates the runtime before model load
    from perch_hoplite.taxonomy import namespace
    from perch_hoplite.zoo.models_onnx import PerchV2OnnxModel

    with labels_path.open("r", encoding="utf-8") as handle:
        classes = namespace.ClassList.from_csv(handle)
    model = PerchV2OnnxModel(
        sample_rate=32000,
        window_size_s=5.0,
        hop_size_s=5.0,
        target_peak=0.25,
        input_name="inputs",
        output_map={"embedding": "embedding", "logits": "label", "frontend": "spectrogram"},
        model_path=str(model_path),
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
        "claim_score": claim_score,
        "claim_rank": claim_rank,
        "top": top,
        "notes": notes,
    }


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
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("canonical archive failed quick_check")
        rows = pending_rows(conn, limit)
        prepared: list[tuple[Any, ...]] = []
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
            prepared.append((
                row["detection_id"], result["status"], "automated independent model",
                MODEL_NAME, result["claim_score"], result["notes"],
                dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            ))
            if verbose:
                print(
                    f"{row['detection_id']} {row['common_name']}: {result['status']} "
                    f"score={result['claim_score']} rank={result['claim_rank']}"
                )
        before = conn.total_changes
        with conn:
            conn.executemany(
                """INSERT INTO reviews
                   (detection_id,status,reviewer,review_model,review_score,notes,reviewed_at)
                   VALUES (?,?,?,?,?,?,?)""",
                prepared,
            )
        reviewed = conn.total_changes - before
    finally:
        conn.close()
    if mirror_path is not None:
        from sync_archive import mirror_archive_db
        mirror_archive_db(db_path, mirror_path)
    return reviewed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DEST / "detections.sqlite3")
    parser.add_argument("--audio-root", type=Path, default=DEFAULT_DEST / "audio")
    parser.add_argument("--assets", type=Path, default=DEFAULT_PERCH)
    parser.add_argument("--mirror", type=Path, default=DEFAULT_MIRROR)
    parser.add_argument("--limit", type=int, default=25, help="0 reviews every pending clip")
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
        reviewed = review_archive(
            args.db, args.audio_root, args.assets,
            None if str(args.mirror) == "" else args.mirror,
            args.limit, args.verbose,
        )
    if args.verbose:
        print(f"reviewed={reviewed}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Perch review failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
