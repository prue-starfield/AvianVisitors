#!/usr/bin/env python3
"""Silent-on-success watchdog for species new to the BirdNET archive."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import sys
from typing import Any
import unicodedata

try:
    from . import corroboration, review_perch
except ImportError:  # Direct script execution from this directory.
    import corroboration  # type: ignore[no-redef]
    import review_perch  # type: ignore[no-redef]

DEFAULT_DB = Path(
    "/Users/prue/Library/Application Support/AvianVisitorsArchive/detections.sqlite3"
)
DEFAULT_STATE = Path(
    "/Users/prue/Library/Application Support/AvianVisitorsArchive/new-species-state.json"
)
DEFAULT_ART_ROOT = Path(
    "/Users/prue/Library/Application Support/AvianVisitorsArchive/illustrations"
)
DEFAULT_MEDIA_ROOT = Path(
    "/Users/prue/.hermes/cache/images/birdnet-species-alerts"
)
ARCHIVE_URL = "https://prudences.tailb1167.ts.net/birds/"
PUBLICATION_MIN_CONFIDENCE = 0.70
MAX_ART_BYTES = 10 * 1024 * 1024
MAX_ART_DIMENSION = 4096
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def safe_notification_metadata(value: Any, *, field: str, maximum: int = 160) -> str:
    """Reject protocol-breaking or invisible controls before message rendering."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise RuntimeError(f"unsafe notification metadata: {field}")
    if len(value) > maximum or any(
        unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        for char in value
    ):
        raise RuntimeError(f"unsafe notification metadata: {field}")
    return value


def markdown(value: str) -> str:
    """Escape Markdown only after transport controls have been rejected."""
    text = safe_notification_metadata(value, field="markdown text")
    for char in ("\\", "*", "_", "`", "~", "|"):
        text = text.replace(char, "\\" + char)
    return text


def artwork_slug(scientific_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(scientific_name).lower()).strip("-")
    if not slug or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise RuntimeError(f"Cannot derive a safe artwork slug for {scientific_name!r}")
    return slug


def prepare_artwork(
    scientific_name: str,
    art_root: Path,
    media_root: Path,
) -> Path:
    """Validate exact-taxon PNG bytes and atomically stage a Discord attachment."""
    slug = artwork_slug(scientific_name)
    root = art_root.expanduser().resolve(strict=True)
    source = (root / f"{slug}.png").resolve(strict=True)
    if source.parent != root or not source.is_file():
        raise RuntimeError(f"Exact artwork is unavailable for {scientific_name}")
    source_size = source.stat().st_size
    if not 24 <= source_size <= MAX_ART_BYTES:
        raise RuntimeError(f"Artwork size is invalid for {scientific_name}")
    with source.open("rb") as handle:
        data = handle.read(MAX_ART_BYTES + 1)
    if len(data) != source_size:
        raise RuntimeError(f"Artwork changed while reading for {scientific_name}")
    if data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        raise RuntimeError(f"Artwork is not a valid PNG for {scientific_name}")
    width, height = struct.unpack(">II", data[16:24])
    if not (1 <= width <= MAX_ART_DIMENSION and 1 <= height <= MAX_ART_DIMENSION):
        raise RuntimeError(f"Artwork dimensions are invalid for {scientific_name}")

    destination_root = media_root.expanduser()
    destination_root.mkdir(parents=True, exist_ok=True)
    os.chmod(destination_root, 0o700)
    destination = destination_root / f"{slug}.png"
    if destination.is_symlink():
        raise RuntimeError(f"Artwork destination is unsafe for {scientific_name}")
    if destination.is_file() and destination.read_bytes() == data:
        return destination.resolve(strict=True)

    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination.resolve(strict=True)


def load_species(
    db_path: Path,
    allowed_labels: frozenset[str] | set[str],
) -> dict[str, dict[str, Any]]:
    if not db_path.is_file():
        raise RuntimeError(f"BirdNET archive mirror is unavailable: {db_path}")
    if not allowed_labels:
        raise RuntimeError("checksum-pinned Perch taxonomy is unavailable")
    uri = db_path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        conn.create_function(
            "valid_current_interpretation",
            7,
            lambda outcome, claim_score, claim_rank, label_count, top_label,
            top_score, provenance: int(corroboration.valid_interpretation(
                outcome, claim_score, claim_rank, label_count, top_label,
                top_score, provenance, allowed_labels,
            )),
            deterministic=True,
        )
        conn.execute("PRAGMA query_only=ON")
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"BirdNET archive mirror failed quick_check: {integrity}")
        rows = conn.execute(
            """WITH published AS (
                   SELECT scientific_name, common_name, observed_at_local,
                          confidence, detection_id
                   FROM detections
                   WHERE confidence BETWEEN ? AND 1.0
                     AND length(detection_id) = 64
                     AND detection_id NOT GLOB '*[^0-9a-f]*'
               ), species_stats AS (
                   SELECT scientific_name, common_name,
                          min(observed_at_local) AS first_heard,
                          max(confidence) AS best_confidence,
                          count(*) AS recognitions
                   FROM published
                   GROUP BY scientific_name, common_name
               ), reviewed AS (
                   SELECT p.*, i.outcome, i.claim_score, i.claim_rank,
                          i.label_count, i.top_label, i.top_score,
                          i.top_score_provenance
                   FROM published p
                   JOIN review_interpretations i USING(detection_id)
                   WHERE i.policy_version = ?
                     AND valid_current_interpretation(
                         i.outcome,i.claim_score,i.claim_rank,i.label_count,
                         i.top_label,i.top_score,i.top_score_provenance
                     ) = 1
               ), review_stats AS (
                   SELECT scientific_name, common_name, count(*) AS reviewed,
                          sum(outcome='corroborated') AS corroborated,
                          sum(outcome='model_conflict') AS model_conflict
                   FROM reviewed
                   GROUP BY scientific_name, common_name
               ), ranked AS (
                   SELECT reviewed.*,
                          row_number() OVER (
                              PARTITION BY scientific_name, common_name
                              ORDER BY CASE outcome
                                  WHEN 'corroborated' THEN 0
                                  WHEN 'uncorroborated' THEN 1
                                  ELSE 2 END,
                                  claim_score DESC, confidence DESC,
                                  observed_at_local, detection_id
                          ) AS evidence_rank
                   FROM reviewed
               )
               SELECT s.scientific_name, s.common_name, s.first_heard,
                      s.best_confidence, s.recognitions,
                      ranked.detection_id AS evidence_id,
                      CASE WHEN rs.corroborated > 0 THEN 'corroborated'
                           WHEN rs.model_conflict = rs.reviewed THEN 'model_conflict'
                           ELSE 'uncorroborated' END AS standing,
                      ranked.claim_score AS review_score,
                      ranked.claim_rank AS review_rank,
                      ranked.label_count AS review_label_count,
                      ranked.top_label AS review_top_label,
                      ranked.top_score AS review_top_score,
                      ranked.top_score_provenance AS review_top_score_provenance
               FROM species_stats s
               JOIN review_stats rs USING(scientific_name, common_name)
               JOIN ranked USING(scientific_name, common_name)
               WHERE ranked.evidence_rank = 1
               ORDER BY s.first_heard, s.scientific_name""",
            (PUBLICATION_MIN_CONFIDENCE, corroboration.POLICY_VERSION),
        ).fetchall()

    species: dict[str, dict[str, Any]] = {}
    for row in rows:
        scientific_name = safe_notification_metadata(
            row["scientific_name"], field="scientific_name",
        )
        if scientific_name not in allowed_labels:
            raise RuntimeError("unsafe notification metadata: unpinned scientific_name")
        common_name = safe_notification_metadata(
            row["common_name"], field="common_name",
        )
        first_heard = safe_notification_metadata(
            row["first_heard"], field="first_heard", maximum=64,
        )
        try:
            dt.datetime.fromisoformat(first_heard)
        except ValueError as exc:
            raise RuntimeError("unsafe notification metadata: first_heard") from exc
        evidence_id = safe_notification_metadata(
            row["evidence_id"], field="evidence_id", maximum=64,
        )
        if not re.fullmatch(r"[0-9a-f]{64}", evidence_id):
            raise RuntimeError("unsafe notification metadata: evidence_id")
        top_label = safe_notification_metadata(
            row["review_top_label"], field="review_top_label",
        )
        if top_label not in allowed_labels:
            raise RuntimeError("unsafe notification metadata: unpinned review_top_label")
        standing = safe_notification_metadata(
            row["standing"], field="standing", maximum=32,
        )
        if standing not in corroboration.OUTCOMES:
            raise RuntimeError("unsafe notification metadata: standing")
        species[scientific_name] = {
            "common_name": common_name,
            "first_heard": first_heard,
            "best_confidence": float(row["best_confidence"]),
            "recognitions": int(row["recognitions"]),
            "evidence_id": evidence_id,
            "standing": standing,
            "review_score": float(row["review_score"]),
            "review_rank": int(row["review_rank"]),
            "review_label_count": int(row["review_label_count"]),
            "review_top_label": top_label,
            "review_top_score": float(row["review_top_score"]),
            "review_top_score_provenance": row[
                "review_top_score_provenance"
            ],
        }
    return species


def read_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"New-species state is unreadable: {path}: {exc}") from exc
    if not isinstance(state.get("seen"), dict):
        raise RuntimeError(f"New-species state has an unsupported schema: {path}")
    if state.get("version") == 1:
        return {
            "version": 2,
            "policy_version": corroboration.POLICY_VERSION,
            "seen": state["seen"],
        }
    if (
        state.get("version") != 2
        or state.get("policy_version") != corroboration.POLICY_VERSION
    ):
        raise RuntimeError(f"New-species state has an unsupported schema: {path}")
    return state


def write_state(path: Path, species: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "policy_version": corroboration.POLICY_VERSION,
        "updated_at": utc_now(),
        "seen": species,
    }
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _validate_alert_details(scientific_name: str, details: dict[str, Any]) -> None:
    safe_notification_metadata(scientific_name, field="scientific_name")
    safe_notification_metadata(details.get("common_name"), field="common_name")
    first_heard = safe_notification_metadata(
        details.get("first_heard"), field="first_heard", maximum=64,
    )
    try:
        dt.datetime.fromisoformat(first_heard)
    except ValueError as exc:
        raise RuntimeError("unsafe notification metadata: first_heard") from exc
    evidence_id = safe_notification_metadata(
        details.get("evidence_id"), field="evidence_id", maximum=64,
    )
    if not re.fullmatch(r"[0-9a-f]{64}", evidence_id):
        raise RuntimeError("unsafe notification metadata: evidence_id")
    safe_notification_metadata(
        details.get("review_top_label"), field="review_top_label",
    )
    try:
        expected = corroboration.classify_outcome(
            details["review_score"], details["review_rank"],
            details["review_top_score"],
        )
        confidence = float(details["best_confidence"])
        recognitions = int(details["recognitions"])
        label_count = int(details["review_label_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("unsafe notification metadata: numeric evidence") from exc
    if (
        not 0 <= confidence <= 1
        or recognitions < 1
        or label_count != corroboration.EXPECTED_LABEL_COUNT
        or details.get("standing") != expected
        or details.get("review_top_score_provenance")
        not in corroboration.TOP_SCORE_PROVENANCES
    ):
        raise RuntimeError("unsafe notification metadata: incoherent evidence")


def alert_message(
    new_species: list[tuple[str, dict[str, Any]]],
    artwork_paths: list[Path],
    media_root: Path = DEFAULT_MEDIA_ROOT,
) -> str:
    standings = {details["standing"] for _name, details in new_species}
    if standings == {"corroborated"}:
        title = "🐦 **New Listening Garden corroborated species**"
    elif standings == {"model_conflict"}:
        title = "⚠️ **New Listening Garden model-conflict candidate**"
    else:
        title = "⚠️ **New Listening Garden reviewed species candidates**"
    lines = [title, ""]
    for scientific_name, details in new_species:
        _validate_alert_details(scientific_name, details)
        score = round(details["best_confidence"] * 100)
        count = details["recognitions"]
        standing = str(details["standing"]).replace("_", " ").upper()
        review_score = details["review_score"] * 100
        top_score = details["review_top_score"] * 100
        provenance = (
            " · alternative score reconstructed from stored 0.1%-rounded notes"
            if details["review_top_score_provenance"]
            == "stored_review_notes_0.1pct" else ""
        )
        alternative = ""
        if details["review_top_label"] != scientific_name:
            alternative = (
                f" · leading alternative {markdown(details['review_top_label'])} "
                f"{top_score:.1f}%"
            )
        lines.extend(
            [
                f"**{markdown(details['common_name'])}** (*{markdown(scientific_name)}*)",
                f"First in the permanent record: `{details['first_heard']} America/New_York`",
                f"Best BirdNET score: **{score}%** · {count} archived recognition{'s' if count != 1 else ''}",
                f"Independent review: **{standing}** · Perch rank #{details['review_rank']:,} "
                f"of {details['review_label_count']:,} · claimed-species model score "
                f"{review_score:.1f}%{alternative}{provenance}",
                f"Inspect this evidence: {ARCHIVE_URL}detection/{details['evidence_id']}",
                "",
            ]
        )
    lines.append(
        "_Each model score—not probability—is an independent classifier output. "
        "Uncorroborated and model-conflict claims remain visible in a separate "
        "candidate section and are not counted as a corroborated field-record species._"
    )
    media_lines: list[str] = []
    if artwork_paths:
        root = media_root.expanduser().resolve(strict=True)
        for path in artwork_paths:
            resolved = path.expanduser().resolve(strict=True)
            if (
                root not in resolved.parents
                or not resolved.is_file()
                or resolved.suffix.lower() != ".png"
            ):
                raise RuntimeError("unsafe notification attachment path")
            path_text = safe_notification_metadata(
                str(resolved), field="attachment path", maximum=1024,
            )
            media_lines.append(f"MEDIA:{path_text}")
    lines.extend(media_lines)
    message = "\n".join(lines)
    allowed_directives = set(media_lines)
    for line in message.splitlines():
        if line.startswith("MEDIA:") and line not in allowed_directives:
            raise RuntimeError("unsafe notification transport directive")
    return message


def run(
    db_path: Path,
    state_path: Path,
    initialize: bool = False,
    art_root: Path = DEFAULT_ART_ROOT,
    media_root: Path = DEFAULT_MEDIA_ROOT,
    allowed_labels: frozenset[str] | set[str] | None = None,
) -> str:
    labels = (
        review_perch.load_pinned_labels(review_perch.DEFAULT_PERCH)
        if allowed_labels is None else allowed_labels
    )
    species = load_species(db_path, labels)
    state = read_state(state_path)
    if initialize or state is None:
        write_state(state_path, species)
        return ""

    seen = state["seen"]
    additions = sorted(
        ((name, details) for name, details in species.items() if name not in seen),
        key=lambda item: (item[1]["first_heard"], item[0]),
    )
    artwork_paths = [
        prepare_artwork(scientific_name, art_root, media_root)
        for scientific_name, _details in additions
    ]
    message = alert_message(additions, artwork_paths, media_root) if additions else ""
    merged = dict(seen)
    merged.update(species)
    write_state(state_path, merged)
    return message


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(os.environ.get("BIRD_ARCHIVE_DB", DEFAULT_DB)),
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=Path(os.environ.get("BIRD_SPECIES_STATE", DEFAULT_STATE)),
    )
    parser.add_argument(
        "--art-root",
        type=Path,
        default=Path(os.environ.get("BIRD_ART_ROOT", DEFAULT_ART_ROOT)),
    )
    parser.add_argument(
        "--media-root",
        type=Path,
        default=Path(os.environ.get("BIRD_ALERT_MEDIA_ROOT", DEFAULT_MEDIA_ROOT)),
    )
    parser.add_argument("--initialize", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    lock_path = args.state.with_name(args.state.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        message = run(
            args.db.expanduser(),
            args.state.expanduser(),
            args.initialize,
            args.art_root.expanduser(),
            args.media_root.expanduser(),
        )
    if message:
        print(message)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"BirdNET new-species watchdog failed: {exc}", file=sys.stderr)
        sys.exit(1)
