"""Versioned, auditable interpretation of independent Perch review outputs."""
from __future__ import annotations

import dataclasses
import math
import re
import sqlite3
from collections.abc import Collection
from typing import Any

PERCH_MODEL_NAME = "Google Perch 2.0 ONNX (inat2024_fsd50k)"
POLICY_VERSION = "perch-corroboration-v2"
EXPECTED_LABEL_COUNT = 14_795
OUTCOMES = frozenset({"corroborated", "uncorroborated", "model_conflict"})
TOP_SCORE_PROVENANCES = frozenset({
    "stored_review_notes_0.1pct",
    "live_model_output_exact",
})

_CONCLUSIONS = (
    "Perch independently supports the BirdNET species claim.",
    "Perch strongly favours another species and does not support the BirdNET claim.",
    "Perch is inconclusive or disagrees; human review remains appropriate.",
)
_TAXON = r"[A-Za-z][A-Za-z_]*(?: [A-Za-z][A-Za-z_-]*){0,2}"
_NOTE_PATTERN = re.compile(
    rf"^(?:{'|'.join(map(re.escape, _CONCLUSIONS))}) "
    r"Claimed species rank (?P<rank>[1-9][0-9]*) of (?P<count>[1-9][0-9]*) "
    r"with score (?P<claim>[0-9]{1,3}\.[0-9])%\. Perch top results: "
    rf"(?P<label1>{_TAXON}) (?P<score1>[0-9]{{1,3}}\.[0-9])%; "
    rf"(?P<label2>{_TAXON}) (?P<score2>[0-9]{{1,3}}\.[0-9])%; "
    rf"(?P<label3>{_TAXON}) (?P<score3>[0-9]{{1,3}}\.[0-9])%\. "
    r"Scores are independent classifier outputs, not calibrated probabilities\.$"
)


@dataclasses.dataclass(frozen=True)
class Interpretation:
    policy_version: str
    outcome: str
    claim_score: float
    claim_rank: int
    label_count: int
    top_label: str
    top_score: float
    top_score_provenance: str


def _score(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("invalid classifier score")
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError("invalid classifier score")
    return score


def _exact_positive_int(value: Any, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("invalid positive integer")
    if not 0 < value <= maximum:
        raise ValueError("invalid positive integer")
    return value


def classify_outcome(claim_score: float, claim_rank: int, top_score: float) -> str:
    """Classify corroboration without pretending sigmoid scores are probabilities."""
    claim = _score(claim_score)
    top = _score(top_score)
    rank = _exact_positive_int(claim_rank, maximum=EXPECTED_LABEL_COUNT)
    if rank == 1 and claim >= 0.25:
        return "corroborated"
    if rank > 3 and claim < 0.10 and top >= 0.25:
        return "model_conflict"
    return "uncorroborated"


def _validate_taxa(labels: Collection[str], allowed_labels: Collection[str]) -> None:
    if not allowed_labels:
        raise ValueError("pinned taxonomy is unavailable")
    if any(label not in allowed_labels for label in labels):
        raise ValueError("Perch label is absent from the pinned taxonomy")


def valid_interpretation(
    outcome: Any,
    claim_score: Any,
    claim_rank: Any,
    label_count: Any,
    top_label: Any,
    top_score: Any,
    top_score_provenance: Any,
    allowed_labels: Collection[str],
) -> bool:
    """One fail-closed boundary shared by writers, APIs, aggregates and alerts."""
    try:
        if not isinstance(outcome, str) or outcome not in OUTCOMES:
            return False
        if not isinstance(claim_score, float) or isinstance(claim_score, bool):
            return False
        if not isinstance(top_score, float) or isinstance(top_score, bool):
            return False
        rank = _exact_positive_int(claim_rank, maximum=EXPECTED_LABEL_COUNT)
        count = _exact_positive_int(label_count, maximum=EXPECTED_LABEL_COUNT)
        if count != EXPECTED_LABEL_COUNT or rank > count:
            return False
        if not isinstance(top_label, str) or top_label not in allowed_labels:
            return False
        if top_score_provenance not in TOP_SCORE_PROVENANCES:
            return False
        expected = classify_outcome(claim_score, rank, top_score)
        return outcome == expected
    except (TypeError, ValueError):
        return False


def interpret_review(
    review_score: float,
    notes: str,
    allowed_labels: Collection[str],
    *,
    claim_label: str | None = None,
) -> Interpretation:
    """Reconstruct a current policy row from immutable, rounded review notes."""
    claim = _score(review_score)
    match = _NOTE_PATTERN.fullmatch(str(notes or ""))
    if match is None:
        raise ValueError("malformed Perch review notes")
    rank = int(match.group("rank"))
    count = int(match.group("count"))
    displayed_claim = float(match.group("claim")) / 100
    top_score = _score(float(match.group("score1")) / 100)
    if rank > count or count != EXPECTED_LABEL_COUNT:
        raise ValueError("invalid Perch rank metadata")
    # Historical notes retain one decimal percentage point. The exact claimed
    # score lives in reviews.review_score; alternative scores are explicitly
    # tagged below as reconstructed from the rounded note.
    if abs(displayed_claim - claim) > 0.00051:
        raise ValueError("Perch note score disagrees with stored review score")
    labels = tuple(match.group(f"label{index}") for index in (1, 2, 3))
    _validate_taxa(labels, allowed_labels)
    if claim_label is not None:
        _validate_taxa((claim_label,), allowed_labels)
    interpretation = Interpretation(
        policy_version=POLICY_VERSION,
        outcome=classify_outcome(claim, rank, top_score),
        claim_score=claim,
        claim_rank=rank,
        label_count=count,
        top_label=labels[0],
        top_score=top_score,
        top_score_provenance="stored_review_notes_0.1pct",
    )
    if not valid_interpretation(
        interpretation.outcome,
        interpretation.claim_score,
        interpretation.claim_rank,
        interpretation.label_count,
        interpretation.top_label,
        interpretation.top_score,
        interpretation.top_score_provenance,
        allowed_labels,
    ):
        raise ValueError("invalid reconstructed Perch interpretation")
    return interpretation


def interpret_result(
    result: dict[str, Any],
    allowed_labels: Collection[str],
) -> Interpretation:
    """Interpret a fresh checksum-pinned inference result before insertion."""
    claim = _score(result["claim_score"])
    rank = int(result["claim_rank"])
    count = int(result["label_count"])
    top = result.get("top") or []
    if rank < 1 or count != EXPECTED_LABEL_COUNT or rank > count or len(top) < 3:
        raise ValueError("invalid Perch inference result")
    labels = tuple(str(item[0]) for item in top[:3])
    if any(not re.fullmatch(_TAXON, label) for label in labels):
        raise ValueError("invalid Perch top label")
    _validate_taxa(labels, allowed_labels)
    claim_label = result.get("claim_label")
    if claim_label is not None:
        _validate_taxa((str(claim_label),), allowed_labels)
    top_score = _score(top[0][1])
    interpretation = Interpretation(
        policy_version=POLICY_VERSION,
        outcome=classify_outcome(claim, rank, top_score),
        claim_score=claim,
        claim_rank=rank,
        label_count=count,
        top_label=labels[0],
        top_score=top_score,
        top_score_provenance="live_model_output_exact",
    )
    if not valid_interpretation(
        interpretation.outcome,
        interpretation.claim_score,
        interpretation.claim_rank,
        interpretation.label_count,
        interpretation.top_label,
        interpretation.top_score,
        interpretation.top_score_provenance,
        allowed_labels,
    ):
        raise ValueError("invalid live Perch interpretation")
    return interpretation


def insert_interpretation(
    conn: sqlite3.Connection,
    detection_id: str,
    interpretation: Interpretation,
    interpreted_at: str,
) -> bool:
    """Insert one policy version exactly once; never replace prior interpretation."""
    cursor = conn.execute(
        """INSERT OR IGNORE INTO review_interpretations (
             detection_id,policy_version,outcome,claim_score,claim_rank,label_count,
             top_label,top_score,top_score_provenance,interpreted_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            detection_id, interpretation.policy_version, interpretation.outcome,
            interpretation.claim_score, interpretation.claim_rank,
            interpretation.label_count, interpretation.top_label,
            interpretation.top_score, interpretation.top_score_provenance,
            interpreted_at,
        ),
    )
    return cursor.rowcount == 1


def backfill_interpretations(
    conn: sqlite3.Connection,
    *,
    interpreted_at: str,
    allowed_labels: Collection[str],
) -> int:
    """Interpret historical pinned Perch reviews missing the current policy row."""
    if not allowed_labels:
        raise RuntimeError("checksum-pinned Perch taxonomy is unavailable")
    rows = conn.execute(
        """SELECT r.detection_id,r.review_score,r.notes,d.scientific_name
           FROM reviews r
           JOIN detections d ON d.detection_id=r.detection_id
           LEFT JOIN review_interpretations i
             ON i.detection_id=r.detection_id AND i.policy_version=?
           WHERE r.review_model=?
             AND typeof(r.review_score)='real'
             AND r.review_score BETWEEN 0.0 AND 1.0
             AND i.detection_id IS NULL
           ORDER BY r.detection_id""",
        (POLICY_VERSION, PERCH_MODEL_NAME),
    ).fetchall()
    prepared = []
    for detection_id, score, notes, claim_label in rows:
        prepared.append((
            detection_id,
            interpret_review(
                score, notes, allowed_labels, claim_label=str(claim_label),
            ),
        ))
    inserted = 0
    for detection_id, interpretation in prepared:
        inserted += int(insert_interpretation(
            conn, detection_id, interpretation, interpreted_at,
        ))
    return inserted
