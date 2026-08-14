"""SQLite storage layer (spec §4).

Schema is written in portable SQL so the move to Postgres is mechanical:
- TEXT timestamps in ISO-8601 UTC (no SQLite-only date funcs in the schema)
- JSON stored as TEXT (Postgres -> JSONB later)
- explicit AUTOINCREMENT avoided in favour of INTEGER PRIMARY KEY

Raw artifacts (screenshots / HTML) live on disk; only their paths/URLs are
stored here, referenced from ``archives`` (spec §4).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
-- Candidate universe (spec §3.1)
CREATE TABLE IF NOT EXISTS candidates (
    candidate_id    TEXT PRIMARY KEY,          -- FEC candidate id
    name            TEXT NOT NULL,
    office          TEXT,                       -- H / S / P
    state           TEXT,
    district        TEXT,
    party           TEXT,
    cycle           INTEGER,
    universe_reason TEXT,                       -- contested_primary | competitive_general |
                                                -- contested_general | nominee | both | '+'-joined combos
    primary_date    TEXT,
    website_url     TEXT,
    url_source      TEXT,                       -- committee | ballotpedia | search | manual
    url_verified    INTEGER NOT NULL DEFAULT 0, -- boolean
    receipts        REAL,
    rating          TEXT,                       -- race rating if from overlay
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Per-state primary calendar (spec §3.2)
CREATE TABLE IF NOT EXISTS elections (
    state            TEXT NOT NULL,
    cycle            INTEGER NOT NULL,
    office           TEXT,                      -- nullable: state-wide primary date
    primary_date     TEXT,
    filing_deadline  TEXT,
    source           TEXT,
    PRIMARY KEY (state, cycle, office)
);

-- One row per fetched page (spec §3.3)
CREATE TABLE IF NOT EXISTS scans (
    scan_id       INTEGER PRIMARY KEY,
    candidate_id  TEXT NOT NULL REFERENCES candidates(candidate_id),
    url           TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    http_status   INTEGER,
    content_type  TEXT,                         -- text/html | application/pdf | ...
    render_mode   TEXT,                         -- http | browser | pdf
    -- (Legacy DBs may carry a deprecated raw_html column — never populated
    -- anymore; full HTML lives on disk via the archiver (spec §3.4) and
    -- `redbox vacuum` reclaims the old rows. Fresh DBs don't create it.)
    raw_text      TEXT,                         -- extracted text the classifier saw
    text_hash     TEXT NOT NULL,                -- sha256 of extracted text
    discovered_via TEXT,                        -- sitemap | common_path | link_crawl | pdf_link
    robots_posture TEXT                         -- respect | override (ROBOTS_POLICY audit)
);
CREATE INDEX IF NOT EXISTS idx_scans_candidate ON scans(candidate_id);
CREATE INDEX IF NOT EXISTS idx_scans_url_hash ON scans(url, text_hash);
-- _prev_scan/_baseline_state filter candidate_id + url IN(...) ORDER BY
-- scan_id DESC; the trailing scan_id lets those resolve without a sort.
CREATE INDEX IF NOT EXISTS idx_scans_cand_url ON scans(candidate_id, url, scan_id);

-- Classifier output (spec §3.5)
CREATE TABLE IF NOT EXISTS detections (
    detection_id  INTEGER PRIMARY KEY,
    scan_id       INTEGER NOT NULL REFERENCES scans(scan_id),
    candidate_id  TEXT NOT NULL REFERENCES candidates(candidate_id),
    classification TEXT NOT NULL,               -- red_box_guidance | ambiguous | no_guidance_detected
    confidence    REAL,
    evidence      TEXT,                         -- JSON: [{quote, why}, ...]
    rationale     TEXT,
    model         TEXT,
    escalated     INTEGER NOT NULL DEFAULT 0,
    classified_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_detections_candidate ON detections(candidate_id);
CREATE INDEX IF NOT EXISTS idx_detections_class ON detections(classification);

-- Preserved evidence (spec §3.4). Files on disk; paths referenced here.
CREATE TABLE IF NOT EXISTS archives (
    archive_id     INTEGER PRIMARY KEY,
    candidate_id   TEXT NOT NULL REFERENCES candidates(candidate_id),
    scan_id        INTEGER REFERENCES scans(scan_id),
    detection_id   INTEGER REFERENCES detections(detection_id),
    url            TEXT NOT NULL,
    screenshot_path TEXT,
    html_path      TEXT,
    text_path      TEXT,
    content_hash   TEXT,
    wayback_url    TEXT,
    wayback_job_id TEXT,
    archived_at    TEXT NOT NULL,
    pdf_path       TEXT
);
CREATE INDEX IF NOT EXISTS idx_archives_candidate ON archives(candidate_id);

-- Schedule E independent expenditures (spec §3.6)
CREATE TABLE IF NOT EXISTS ie_filings (
    ie_id          INTEGER PRIMARY KEY,
    candidate_id   TEXT NOT NULL REFERENCES candidates(candidate_id),
    committee_id   TEXT,
    committee_name TEXT,
    payee_name     TEXT,
    support_oppose_indicator TEXT,              -- S | O
    expenditure_amount REAL,
    expenditure_date   TEXT,
    dissemination_date TEXT,
    cycle          INTEGER,
    transaction_id TEXT,
    raw            TEXT,                         -- JSON of source row
    UNIQUE (transaction_id, committee_id)
);
CREATE INDEX IF NOT EXISTS idx_ie_candidate ON ie_filings(candidate_id);

-- "Guidance posted -> money appeared" correlation (spec §3.6)
CREATE TABLE IF NOT EXISTS corroboration (
    corroboration_id INTEGER PRIMARY KEY,
    candidate_id     TEXT NOT NULL REFERENCES candidates(candidate_id),
    detection_id     INTEGER REFERENCES detections(detection_id),
    guidance_first_detected TEXT,
    supporting_ie_total_after REAL,             -- S IE dollars dated after detection
    supporting_total REAL,                       -- all S IE this cycle
    opposing_total   REAL,                       -- all O IE this cycle
    ie_filing_count  INTEGER,
    spender_list     TEXT,                       -- JSON: [{committee_id, committee_name, indicator, amount, count, first_date, last_date}]
    computed_at      TEXT NOT NULL
);

-- Human review gate (spec §3.7)
CREATE TABLE IF NOT EXISTS reviews (
    review_id     INTEGER PRIMARY KEY,
    detection_id  INTEGER NOT NULL REFERENCES detections(detection_id),
    reviewer      TEXT,
    action        TEXT NOT NULL,                -- approve | reject | needs_more
    notes         TEXT,
    reviewed_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviews_detection ON reviews(detection_id);

-- Put-up / take-down change events (spec §3.2): a red box is often removed the
-- moment it draws attention; the transition is itself a publishable signal.
CREATE TABLE IF NOT EXISTS change_events (
    change_id     INTEGER PRIMARY KEY,
    candidate_id  TEXT NOT NULL REFERENCES candidates(candidate_id),
    url           TEXT NOT NULL,
    event_type    TEXT NOT NULL,                -- put_up | take_down | modified
    prev_scan_id  INTEGER REFERENCES scans(scan_id),
    new_scan_id   INTEGER REFERENCES scans(scan_id),
    prev_classification TEXT,
    new_classification  TEXT,
    detected_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_change_candidate ON change_events(candidate_id);

-- NOTE: the spec §3.8 `publications` table was never used — publish state
-- lives entirely in `reviews` (latest review wins) and the built site.
-- Fresh databases no longer create it; legacy DBs keep the empty table.
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with sane defaults (FK enforcement, Row access)."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False + a busy timeout so concurrent scan-all workers
    # (each its own connection) serialize writes instead of failing with
    # "database is locked". WAL lets readers proceed during a writer.
    conn = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


# Columns added after the initial schema shipped. init_db adds any that are
# missing from an existing database (SQLite ALTER TABLE ADD COLUMN), so dev DBs
# upgrade without a manual migration.
_MIGRATIONS: dict[str, dict[str, str]] = {
    "corroboration": {
        "supporting_total": "REAL",
        "opposing_total": "REAL",
        "ie_filing_count": "INTEGER",
    },
    "candidates": {
        # Last scan disposition: NULL=unscanned, 'scanned', 'robots_blocked'.
        "scan_status": "TEXT",
        # ISO-8601 UTC time the last scan ATTEMPT concluded — stamped alongside
        # scan_status on every attempt, including zero-page outcomes
        # (robots_blocked / fetch_failed) that write no `scans` rows. The
        # scheduler keys cadence off MAX(scans.fetched_at, last_attempt_at) so
        # a blocked/unreachable site isn't "never scanned" and re-hit daily.
        "last_attempt_at": "TEXT",
        # general/full modes: how the nominee was confirmed
        # (uncontested | feed:<name> | manual). NULL for primary-mode rows.
        "nominee_source": "TEXT",
        # Not actually running for this seat: NULL/0=active, 1=FEC
        # candidate_inactive (withdrawn/superseded — e.g. a House member now
        # running for Senate), 2=human call from the review console, 3=lost the
        # primary (results feed via `mark-primary-losers`). Inactive rows are
        # kept for history but excluded from resolve/scan/publish; mark-inactive
        # refreshes only value 1, so 2/3 are never clobbered by an FEC sync.
        "inactive": "INTEGER",
        # Whether the last concluded attempt was truncated by the candidate
        # wall-clock ceiling (1) or ran to completion (0) — stamped alongside
        # scan_status/last_attempt_at on every attempt, so an operator can
        # tell a partial sweep from a full one. NULL on rows predating the
        # column (disposition unknown).
        "last_scan_partial": "INTEGER",
    },
    "elections": {
        # Statewide primary-runoff date (ISO-8601), where the state holds one
        # (transcribed from the calendar fixture's runoff_date field). A race
        # in a runoff state isn't settled — and its losers aren't knowable —
        # until the RUNOFF has passed, not the first-round date.
        "runoff_date": "TEXT",
    },
    "archives": {
        # Raw document preserved when the detection source was a PDF; the
        # screenshot_path then holds a rasterization of its pages.
        "pdf_path": "TEXT",
    },
    "scans": {
        # 'respect' | 'override': how robots.txt was applied to this fetch.
        # ROBOTS_POLICY.md promises every override-collected page is
        # auditable; NULL on rows predating the column.
        "robots_posture": "TEXT",
    },
}


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, cols in _MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Create the schema if absent, apply column migrations, return a connection."""
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    _apply_migrations(conn)
    return conn
