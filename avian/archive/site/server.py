#!/usr/bin/env python3
"""Tailnet-only web service for the append-only BirdNET archive.

Serves a read-only API, static field-journal UI, evidence audio by detection ID,
and a local illustration mirror. It binds loopback by default; Caddy/Tailscale
is the intended security boundary.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable, Optional, Sequence
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

HEX_ID = re.compile(r"^[0-9a-f]{64}$")
SAFE_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){1,4}(?:-2)?$")
TZ = ZoneInfo("America/New_York")
PUBLICATION_MIN_CONFIDENCE = 0.70


def now_local() -> dt.datetime:
    return dt.datetime.now(TZ)


def slugify(scientific_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", scientific_name.lower()).strip("-")


def public_model_label(value: Any) -> str:
    """Return a display label without leaking POSIX or Windows path prefixes."""
    return re.split(r"[\\/]", str(value or ""))[-1]


def db_connect(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def rows_dict(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def get_summary(db_path: Path) -> dict[str, Any]:
    today = now_local().date().isoformat()
    seven_start = (now_local().date() - dt.timedelta(days=6)).isoformat()
    previous_start = (now_local().date() - dt.timedelta(days=13)).isoformat()
    previous_end = (now_local().date() - dt.timedelta(days=7)).isoformat()
    with db_connect(db_path) as db:
        totals = dict(db.execute(
            """SELECT count(*) AS detections,
                      count(DISTINCT scientific_name) AS species,
                      count(DISTINCT date) AS days_listening,
                      min(observed_at_local) AS first_record,
                      max(observed_at_local) AS latest_record,
                      sum(audio_sha256 IS NOT NULL) AS clips_preserved
               FROM detections WHERE confidence >= ?""",
            (PUBLICATION_MIN_CONFIDENCE,),
        ).fetchone())
        today_row = dict(db.execute(
            """SELECT count(*) AS detections,
                      count(DISTINCT scientific_name) AS species
               FROM detections WHERE date=? AND confidence >= ?""",
            (today, PUBLICATION_MIN_CONFIDENCE),
        ).fetchone())
        recent = db.execute(
            """SELECT
                 sum(CASE WHEN date BETWEEN ? AND ? THEN 1 ELSE 0 END) AS last_7,
                 sum(CASE WHEN date BETWEEN ? AND ? THEN 1 ELSE 0 END) AS previous_7
               FROM detections WHERE confidence >= ?""",
            (seven_start, today, previous_start, previous_end,
             PUBLICATION_MIN_CONFIDENCE),
        ).fetchone()
        sync = db.execute(
            """SELECT completed_at, source_rows, inserted_rows, indexed_clips,
                      snapshot_sha256
               FROM sync_runs WHERE status='ok'
               ORDER BY run_id DESC LIMIT 1"""
        ).fetchone()
        pending = db.execute(
            "SELECT count(*) FROM review_queue WHERE confidence >= ?",
            (PUBLICATION_MIN_CONFIDENCE,),
        ).fetchone()[0]
        reviewed = db.execute(
            """SELECT count(*) FROM reviews r JOIN detections d USING(detection_id)
               WHERE d.confidence >= ?""",
            (PUBLICATION_MIN_CONFIDENCE,),
        ).fetchone()[0]
    last_7 = int(recent["last_7"] or 0)
    previous_7 = int(recent["previous_7"] or 0)
    delta = None if previous_7 == 0 else round((last_7 - previous_7) / previous_7 * 100, 1)
    return {
        "totals": totals,
        "today": {**today_row, "date": today},
        "trend": {"last_7": last_7, "previous_7": previous_7, "percent_change": delta},
        "review": {"pending": pending, "reviewed": reviewed},
        "latest_sync": dict(sync) if sync else None,
        "policy": {
            "name": "publication-v1",
            "description": "Accepted BirdNET detections for the local calendar day",
            "minimum_confidence": PUBLICATION_MIN_CONFIDENCE,
            "timezone": "America/New_York",
            "frame_parity": True,
        },
        "generated_at": now_local().isoformat(timespec="seconds"),
    }


def get_today(db_path: Path) -> dict[str, Any]:
    today = now_local().date().isoformat()
    with db_connect(db_path) as db:
        rows = rows_dict(db.execute(
            """SELECT scientific_name, common_name, count(*) AS detections,
                      min(time) AS first_heard, max(time) AS last_heard,
                      max(confidence) AS best_confidence,
                      avg(confidence) AS mean_confidence,
                      sum(audio_sha256 IS NOT NULL) AS clips_preserved
               FROM detections WHERE date=? AND confidence >= ?
               GROUP BY scientific_name, common_name
               ORDER BY detections DESC, common_name""",
            (today, PUBLICATION_MIN_CONFIDENCE),
        ))
    for row in rows:
        row["slug"] = slugify(row["scientific_name"])
    return {
        "date": today,
        "timezone": "America/New_York",
        "publication_policy": "publication-v1",
        "species": rows,
    }


def get_activity(db_path: Path, days: int) -> dict[str, Any]:
    days = max(7, min(days, 730))
    start = (now_local().date() - dt.timedelta(days=days - 1)).isoformat()
    with db_connect(db_path) as db:
        daily = rows_dict(db.execute(
            """SELECT date, count(*) AS detections,
                      count(DISTINCT scientific_name) AS species,
                      max(confidence) AS best_confidence
               FROM detections WHERE date>=? AND confidence >= ?
               GROUP BY date ORDER BY date""",
            (start, PUBLICATION_MIN_CONFIDENCE),
        ))
        hourly = rows_dict(db.execute(
            """SELECT CAST(substr(time,1,2) AS INTEGER) AS hour,
                      count(*) AS detections
               FROM detections WHERE date>=? AND confidence >= ?
               GROUP BY hour ORDER BY hour""",
            (start, PUBLICATION_MIN_CONFIDENCE),
        ))
    return {"days": days, "start": start, "daily": daily, "by_hour": hourly}


def get_species(db_path: Path) -> dict[str, Any]:
    with db_connect(db_path) as db:
        species = rows_dict(db.execute(
            """SELECT scientific_name, common_name, count(*) AS detections,
                      count(DISTINCT date) AS days_heard,
                      min(observed_at_local) AS first_heard,
                      max(observed_at_local) AS last_heard,
                      max(confidence) AS best_confidence,
                      avg(confidence) AS mean_confidence
               FROM detections WHERE confidence >= ?
               GROUP BY scientific_name, common_name
               ORDER BY days_heard DESC, detections DESC, common_name""",
            (PUBLICATION_MIN_CONFIDENCE,),
        ))
    for row in species:
        row["slug"] = slugify(row["scientific_name"])
    return {"species": species}


def get_seasonality(db_path: Path) -> dict[str, Any]:
    with db_connect(db_path) as db:
        rows = rows_dict(db.execute(
            """SELECT CAST(strftime('%m',date) AS INTEGER) AS calendar_month,
                      scientific_name, common_name, count(*) AS detections,
                      count(DISTINCT date) AS days_heard,
                      count(DISTINCT substr(date,1,4)) AS years_observed
               FROM detections WHERE confidence >= ?
               GROUP BY calendar_month,scientific_name,common_name
               ORDER BY scientific_name,calendar_month""",
            (PUBLICATION_MIN_CONFIDENCE,),
        ))
        monthly = rows_dict(db.execute(
            """SELECT substr(date,1,7) AS month, count(*) AS detections,
                      count(DISTINCT scientific_name) AS species,
                      count(DISTINCT date) AS listening_days
               FROM detections WHERE confidence >= ?
               GROUP BY substr(date,1,7) ORDER BY month""",
            (PUBLICATION_MIN_CONFIDENCE,),
        ))
    return {"by_species_month": rows, "timeline": monthly}


def get_epochs(db_path: Path) -> dict[str, Any]:
    with db_connect(db_path) as db:
        epochs = rows_dict(db.execute(
            """SELECT effective_at, audio_model, range_model,
                      occurrence_threshold, confidence_threshold,
                      sensitivity, overlap, reason
               FROM configuration_epochs ORDER BY effective_at"""
        ))
    for epoch in epochs:
        epoch["audio_model"] = public_model_label(epoch.get("audio_model"))
        epoch["range_model"] = public_model_label(epoch.get("range_model"))
    return {"epochs": epochs}


def get_detections(
    db_path: Path,
    params: dict[str, list[str]],
    audio_root: Optional[Path] = None,
) -> dict[str, Any]:
    def one(name: str, default: str = "") -> str:
        return (params.get(name) or [default])[0].strip()

    try:
        limit = max(1, min(int(one("limit", "75")), 200))
        offset = max(0, int(one("offset", "0")))
        confidence_min = max(
            PUBLICATION_MIN_CONFIDENCE,
            min(float(one("confidence_min", str(PUBLICATION_MIN_CONFIDENCE))), 1.0),
        )
    except ValueError as exc:
        raise ValueError("invalid numeric filter") from exc
    conditions = [
        "d.confidence >= ?",
        "length(d.detection_id) = 64",
        "d.detection_id NOT GLOB '*[^0-9a-f]*'",
    ]
    values: list[Any] = [confidence_min]
    species = one("species")
    date_from, date_to = one("date_from"), one("date_to")
    review = one("review")
    query = one("q")
    if species:
        conditions.append("d.scientific_name = ?")
        values.append(species)
    for label, value, op in (("date_from", date_from, ">="), ("date_to", date_to, "<=")):
        if value:
            try:
                dt.date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"invalid {label}") from exc
            conditions.append(f"d.date {op} ?")
            values.append(value)
    if query:
        conditions.append("(d.common_name LIKE ? OR d.scientific_name LIKE ?)")
        like = "%" + query[:80].replace("%", "") + "%"
        values += [like, like]
    status_expr = "COALESCE(r.status, CASE WHEN d.confidence < 0.90 THEN 'pending' ELSE 'unreviewed' END)"
    if review:
        if review not in {"pending", "unreviewed", "confirmed", "rejected", "uncertain"}:
            raise ValueError("invalid review filter")
        conditions.append(status_expr + " = ?")
        values.append(review)
    where = " AND ".join(conditions)
    with db_connect(db_path) as db:
        total = db.execute(
            f"SELECT count(*) FROM detections d LEFT JOIN reviews r USING(detection_id) WHERE {where}",
            values,
        ).fetchone()[0]
        result = rows_dict(db.execute(
            f"""SELECT d.detection_id, d.date, d.time, d.observed_at_local,
                       d.timezone, d.scientific_name, d.common_name, d.confidence,
                       d.ingested_at,
                       d.audio_relpath, d.audio_sha256, d.audio_bytes,
                       {status_expr} AS review_status,
                       r.reviewer, r.review_model, r.review_score, r.notes, r.reviewed_at,
                       c.occurrence_prior, c.local_prior, c.repetition_count,
                       c.posterior, c.algorithm_version
                FROM detections d
                LEFT JOIN reviews r USING(detection_id)
                LEFT JOIN context_scores c USING(detection_id)
                WHERE {where}
                ORDER BY d.date DESC, d.time DESC
                LIMIT ? OFFSET ?""",
            values + [limit, offset],
        ))
    for row in result:
        relpath = row.pop("audio_relpath", None)
        archived = bool(row.pop("audio_sha256", None))
        if archived and relpath and audio_root is not None:
            root = audio_root.resolve()
            candidate = (root / relpath).resolve()
            row["has_audio"] = root in candidate.parents and candidate.is_file()
        else:
            row["has_audio"] = archived
        row["slug"] = slugify(row["scientific_name"])
    return {"total": total, "limit": limit, "offset": offset, "detections": result}


class ArchiveHandler(BaseHTTPRequestHandler):
    server_version = "ListeningGarden/1.0"

    @property
    def config(self) -> argparse.Namespace:
        return self.server.config  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} [{self.log_date_time_string()}] {format % args}")

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; media-src 'self'; connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
        )
        super().end_headers()

    def json_response(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def error_json(self, message: str, status: int = 400) -> None:
        self.json_response({"error": message}, status)

    def range_not_satisfiable(self, size: int) -> None:
        self.send_response(416)
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def serve_file(self, path: Path, content_type: Optional[str] = None,
                   cache_control: str = "no-cache") -> None:
        if not path.is_file():
            self.error_json("not found", 404)
            return
        # Open before headers: a permission or media failure must produce one
        # coherent error response, not a partial 200 followed by a second reply.
        with path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            start, end = 0, size - 1
            partial = False
            range_header = self.headers.get("Range", "")
            if range_header:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
                if not match or (not match.group(1) and not match.group(2)):
                    self.range_not_satisfiable(size)
                    return
                first, last = match.group(1), match.group(2)
                if not first and last:
                    start = max(0, size - int(last))
                    end = size - 1
                else:
                    if first:
                        start = int(first)
                    if last:
                        end = int(last)
                if start > end or start >= size:
                    self.range_not_satisfiable(size)
                    return
                end = min(end, size - 1)
                partial = True
            length = end - start + 1
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", cache_control)
            self.send_header("Accept-Ranges", "bytes")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(1024 * 256, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        params = parse_qs(parsed.query)
        try:
            if path == "/health":
                with db_connect(self.config.db) as db:
                    count = db.execute("SELECT count(*) FROM detections").fetchone()[0]
                    integrity = db.execute("PRAGMA quick_check").fetchone()[0]
                self.json_response({
                    "status": "ok" if integrity == "ok" else "degraded",
                    "service": "listening-garden",
                    "detections": count,
                    "database": integrity,
                    "audio_mounted": self.config.audio.exists(),
                    "art_mounted": self.config.art.exists(),
                })
            elif path == "/api/summary":
                self.json_response(get_summary(self.config.db))
            elif path == "/api/today":
                self.json_response(get_today(self.config.db))
            elif path == "/api/activity":
                try:
                    days = int((params.get("days") or ["365"])[0])
                except ValueError:
                    raise ValueError("invalid days")
                self.json_response(get_activity(self.config.db, days))
            elif path == "/api/species":
                self.json_response(get_species(self.config.db))
            elif path == "/api/seasonality":
                self.json_response(get_seasonality(self.config.db))
            elif path == "/api/epochs":
                self.json_response(get_epochs(self.config.db))
            elif path == "/api/detections":
                self.json_response(get_detections(self.config.db, params, self.config.audio))
            elif path.startswith("/api/audio/"):
                did = path.rsplit("/", 1)[-1]
                if not HEX_ID.fullmatch(did):
                    self.error_json("invalid detection id", 400)
                    return
                with db_connect(self.config.db) as db:
                    row = db.execute(
                        """SELECT audio_relpath FROM detections
                           WHERE detection_id=? AND audio_sha256 IS NOT NULL
                             AND confidence >= ?""",
                        (did, PUBLICATION_MIN_CONFIDENCE),
                    ).fetchone()
                if not row:
                    self.error_json("audio unavailable", 404)
                    return
                root = self.config.audio.resolve()
                candidate = (root / row["audio_relpath"]).resolve()
                if root not in candidate.parents:
                    self.error_json("invalid archive path", 500)
                    return
                self.serve_file(candidate, "audio/mpeg", "private, max-age=86400")
            elif path.startswith("/art/"):
                name = path.rsplit("/", 1)[-1]
                if not name.endswith(".png") or not SAFE_SLUG.fullmatch(name[:-4]):
                    self.error_json("invalid illustration", 400)
                    return
                self.serve_file(self.config.art / name, "image/png", "private, max-age=86400")
            elif path.startswith("/api/"):
                self.error_json("API endpoint not found", 404)
            else:
                static_name = "index.html" if path in {"", "/"} else path.lstrip("/")
                if static_name not in {"index.html", "app.js", "styles.css", "favicon.svg"}:
                    static_name = "index.html"
                content_type = {
                    "index.html": "text/html; charset=utf-8",
                    "app.js": "application/javascript; charset=utf-8",
                    "styles.css": "text/css; charset=utf-8",
                    "favicon.svg": "image/svg+xml",
                }[static_name]
                self.serve_file(self.config.static / static_name, content_type, "no-cache, must-revalidate")
        except ValueError as exc:
            self.error_json(str(exc), 400)
        except (sqlite3.Error, OSError) as exc:
            print(f"request failed: {exc}")
            self.error_json("archive temporarily unavailable", 503)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=os.environ.get("BIRD_ARCHIVE_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("BIRD_ARCHIVE_PORT", "9137")))
    ap.add_argument("--db", type=Path, default=Path(os.environ.get(
        "BIRD_ARCHIVE_DB",
        "~/Library/Application Support/AvianVisitorsArchive/detections.sqlite3",
    )).expanduser())
    ap.add_argument("--audio", type=Path, default=Path(os.environ.get(
        "BIRD_ARCHIVE_AUDIO",
        "~/Library/Application Support/AvianVisitorsArchive/audio",
    )))
    ap.add_argument("--art", type=Path, default=Path(os.environ.get(
        "BIRD_ARCHIVE_ART",
        "~/Library/Application Support/AvianVisitorsArchive/illustrations",
    )))
    ap.add_argument("--static", type=Path, default=here / "static")
    args = ap.parse_args(argv)
    args.db = args.db.expanduser().resolve()
    args.audio = args.audio.expanduser().resolve()
    args.art = args.art.expanduser().resolve()
    args.static = args.static.expanduser().resolve()
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if not args.db.is_file():
        raise SystemExit(f"archive database not found: {args.db}")
    server = ThreadingHTTPServer((args.host, args.port), ArchiveHandler)
    server.config = args  # type: ignore[attr-defined]
    print(f"Listening Garden on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
