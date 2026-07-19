# Append-only BirdNET archive

`sync_archive.py` turns a BirdNET-Pi installation into an auditable long-term
record suitable for seasonality analysis.

It does five things on each run:

1. Uses SQLite's online backup API to take a consistent snapshot of
   `birds.db` without stopping detection.
2. Imports immutable detector claims into a separate, idempotent SQLite
   archive.
3. Mirrors the MP3 evidence clips before BirdNET's disk-pressure purge can
   remove them.
4. Links each row to its clip using a SHA-256 content digest.
5. Refreshes a checksum-verified, size-bounded internal playback cache for the
   tailnet web UI. This cache is disposable; the external archive remains the
   canonical evidence store.

Later reviews and contextual/Bayesian scores are stored in separate tables.
They never overwrite the original species, confidence, model settings, or
recording filename.

Detector/range-model changes belong in `configuration_epochs`, so historical
analysis can distinguish genuine seasonal changes from a classifier or
threshold change.

## Default destination

```text
/Volumes/Crucial Data/Hermes/bird-archive/
├── detections.sqlite3
└── audio/By_Date/...
```

After every successful sync, the SQLite archive is also copied atomically to
the Mac's internal SSD at:

```text
~/Library/Application Support/AvianVisitorsArchive/detections.sqlite3
```

That protects the seasonality record across failure of either the Pi/external
disk or the Mac's internal storage. The mirror receives an adjacent SHA-256
manifest and must pass SQLite's full integrity check before publication.

The read-only website uses two bounded internal presentation caches:

```text
~/Library/Application Support/AvianVisitorsArchive/
├── audio/          # newest verified clips, mechanically capped at 2 GiB
└── illustrations/  # local copy of the current 333-species art library
```

The audio cache avoids granting a generic Python interpreter broad removable-
volume access. Clips outside the newest 2 GiB remain permanent in the external
archive but are not immediately playable in the browser.

Override it with `--dest`. The destination's parent must already exist; this
makes an unmounted external drive fail loudly rather than silently writing a
lookalike directory on the system disk.

## Run

```bash
python3 avian/archive/sync_archive.py --verbose
```

The default network route expects the microphone Pi at `192.168.36.9`, reached
through the frame Pi at `192.168.36.36`. All connection paths and model labels
are command-line options. Normal successful runs produce no output, which makes
the script suitable for silent-on-success watchdog scheduling.

## Private archive website

`site/server.py` is a standard-library, loopback-only HTTP service installed as
the `com.prue.birdarchive` LaunchAgent. Caddy exposes it only through the
Tailscale-authenticated route:

```text
https://prudences.tailb1167.ts.net/birds/
```

The service opens the internal SQLite mirror in query-only mode. It never
returns raw coordinates or filesystem paths. Evidence audio is resolved only
from an immutable 64-character detection ID and supports HTTP byte ranges.
The website's “Today” species set uses publication policy v1: accepted BirdNET
detections for the `America/New_York` calendar day, matching the e-ink frame.
The mirror may trail the live microphone by one archive-sync interval, which is
displayed prominently rather than hidden.

## Independent Google Perch review

`review_perch.py` runs Google Perch 2 locally through the official ONNX export.
It verifies each archived clip's byte count and SHA-256 digest before inference,
then writes an insert-only row to `reviews`; BirdNET's original species and raw
score are never changed. Routine inference uses pinned, checksum-verified model
weights and labels under `~/Library/Application Support/AvianVisitorsArchive/perch/`
and sends no household audio to a cloud service. The trusted model and label
digests are pinned in version-controlled code; the adjacent manifest is an audit
copy, not the trust anchor.

The automated conclusion policy is deliberately conservative because Perch
classifier outputs are not calibrated probabilities:

- `confirmed` — the BirdNET species is Perch's top result at 25% or higher;
- `rejected` — a different species scores at least 75%, while the BirdNET species
  is below 10% and outside Perch's top three;
- `uncertain` — every other result, including close sibling-species disagreements.

The archive website's evidence panel displays the exact clip, immutable digest,
BirdNET score, Perch status/score/rank, and top alternatives. A deep link uses the
immutable detection ID: `/birds/detection/<64 hex characters>`. Deployments may
retain a narrowly validated redirect from the former `?detection=` URL while old
bookmarks age out.

## Archive views

- `daily_species` — first/last heard, count, and best confidence per day
- `monthly_species` — monthly count and number of days heard
- `seasonality_by_month` — calendar-month totals across years
- `review_queue` — detections below 90% with no secondary review yet

Example:

```bash
sqlite3 -header -column \
  '/Volumes/Crucial Data/Hermes/bird-archive/detections.sqlite3' \
  'SELECT * FROM monthly_species ORDER BY month, detections DESC;'
```

## Integrity and recovery

- Every source snapshot must pass `PRAGMA integrity_check` before import.
- Source rows are keyed by a hash of their complete immutable payload, so
  repeated syncs and restored/VACUUMed source databases cannot duplicate them.
- MP3s are copied with `--ignore-existing` and then content-hashed.
- Each run is recorded in `sync_runs`, including source/insert counts, snapshot
  hash, completion status, and any error.
- The archive database is mirrored to a second physical device using SQLite's
  backup API and an atomic rename.
- A non-blocking file lock prevents overlapping scheduled runs.
