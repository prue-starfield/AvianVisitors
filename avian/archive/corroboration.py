"""Versioned, auditable interpretation of independent Perch review outputs."""
from __future__ import annotations

import dataclasses
import math
import re
import sqlite3
from typing import Any

PERCH_MODEL_NAME = "Google Perch 2.0 ONNX (inat2024_fsd50k)"
POLICY_VERSION = "perch-corroboration-v2"
OUTCOMES = frozenset({"corroborated", "uncorroborated", "model_conflict"})

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
    rf"(?P<top_label>{_TAXON}) (?P<top_score>[0-9]{{1,3}}\.[0-9])%; "
    rf"{_TAXON} [0-9]{{1,3}}\.[0-9]%; {_TAXON} [0-9]{{1,3}}\.[0-9]%\. "
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


def _score(value: Any) -> float:
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError("invalid classifier score")
    return score


def classify_outcome(claim_score: float, claim_rank: int, top_score: float) -> str:
    """Classify corroboration without pretending sigmoid scores are probabilities."""
    claim = _score(claim_score)
    top = _score(top_score)
    if not isinstance(claim_rank, int) or claim_rank < 1:
        raise ValueError("invalid claimed-species rank")
    if claim_rank == 1 and claim >= 0.25:
        return "corroborated"
    if claim_rank > 3 and claim < 0.10 and top >= 0.25:
        return "model_conflict"
    return "uncorroborated"


def interpret_review(review_score: float, notes: str) -> Interpretation:
    """Reconstruct a policy row from the checksum-pinned reviewer's stored note."""
    claim = _score(review_score)
    match = _NOTE_PATTERN.fullmatch(str(notes or ""))
    if match is None:
        raise ValueError("malformed Perch review notes")
    rank = int(match.group("rank"))
    count = int(match.group("count"))
    displayed_claim = float(match.group("claim")) / 100
    top_score = _score(float(match.group("top_score")) / 100)
    if rank > count or count > 100_000:
        raise ValueError("invalid Perch rank metadata")
    # Historical notes retain one decimal percentage point. Ensure they agree
    # with the full-precision review_score before trusting the remaining fields.
    if abs(displayed_claim - claim) > 0.00051:
        raise ValueError("Perch note score disagrees with stored review score")
    top_label = match.group("top_label")
    return Interpretation(
        policy_version=POLICY_VERSION,
        outcome=classify_outcome(claim, rank, top_score),
        claim_score=claim,
        claim_rank=rank,
        label_count=count,
        top_label=top_label,
        top_score=top_score,
    )


def interpret_result(result: dict[str, Any]) -> Interpretation:
    """Interpret a fresh inference result before it is inserted."""
    claim = _score(result["claim_score"])
    rank = int(result["claim_rank"])
    count = int(result["label_count"])
    top = result.get("top") or []
    if rank < 1 or count < rank or count > 100_000 or not top:
        raise ValueError("invalid Perch inference result")
    top_label = str(top[0][0])
    if not re.fullmatch(_TAXON, top_label):
        raise ValueError("invalid Perch top label")
    top_score = _score(top[0][1])
    return Interpretation(
        policy_version=POLICY_VERSION,
        outcome=classify_outcome(claim, rank, top_score),
        claim_score=claim,
        claim_rank=rank,
        label_count=count,
        top_label=top_label,
        top_score=top_score,
    )


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
             top_label,top_score,interpreted_at
           ) VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            detection_id, interpretation.policy_version, interpretation.outcome,
            interpretation.claim_score, interpretation.claim_rank,
            interpretation.label_count, interpretation.top_label,
            interpretation.top_score, interpreted_at,
        ),
    )
    return cursor.rowcount == 1


def backfill_interpretations(
    conn: sqlite3.Connection,
    *,
    interpreted_at: str,
) -> int:
    """Interpret every historical pinned Perch review missing the current policy row."""
    rows = conn.execute(
        """SELECT r.detection_id,r.review_score,r.notes
           FROM reviews r
           LEFT JOIN review_interpretations i
             ON i.detection_id=r.detection_id AND i.policy_version=?
           WHERE r.review_model=? AND r.review_score BETWEEN 0.0 AND 1.0
             AND i.detection_id IS NULL
           ORDER BY r.detection_id""",
        (POLICY_VERSION, PERCH_MODEL_NAME),
    ).fetchall()
    prepared = []
    for detection_id, score, notes in rows:
        prepared.append((detection_id, interpret_review(score, notes)))
    inserted = 0
    for detection_id, interpretation in prepared:
        inserted += int(insert_interpretation(
            conn, detection_id, interpretation, interpreted_at,
        ))
    return inserted
