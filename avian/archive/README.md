# Append-only BirdNET archive

`sync_archive.py` turns a BirdNET-Pi installation into an auditable long-term
record suitable for seasonality analysis.

It does four things on each run:

1. Uses SQLite's online backup API to take a consistent snapshot of
   `birds.db` without stopping detection.
2. Imports immutable detector claims into a separate, idempotent SQLite
   archive.
3. Mirrors the MP3 evidence clips before BirdNET's disk-pressure purge can
   remove them.
4. Links each row to its clip using a SHA-256 content digest.

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
