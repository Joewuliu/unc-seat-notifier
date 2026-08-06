-- Current-state row per watched section. Only ever written by a successful fetch
-- (seat/waitlist/status fields) or a failure counter bump (consecutive_failures,
-- last_attempt_at only). Never both from the same event.
--
-- pending_notification_since / last_notification_attempt_at model an outstanding
-- notification explicitly: non-NULL means a transition into availability was detected
-- and the most recent delivery attempt (if any) hasn't succeeded yet. Both are cleared
-- back to NULL the moment a send succeeds or the section becomes unavailable again.
-- last_notified_at only ever advances on a *successful* send.
CREATE TABLE IF NOT EXISTS section_state (
    term TEXT NOT NULL,
    subject TEXT NOT NULL,
    catalog_number TEXT NOT NULL,
    section TEXT NOT NULL,
    seats_available INTEGER NOT NULL DEFAULT 0,
    seats_capacity INTEGER NOT NULL DEFAULT 0,
    -- NULL means "this data source doesn't expose waitlist data" - true for UNC's
    -- real Tiled Search results - not "confirmed zero people waiting."
    waitlist_available INTEGER,
    waitlist_capacity INTEGER,
    status TEXT NOT NULL DEFAULT 'unknown',
    last_seen_at TEXT,
    last_notified_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    pending_notification_since TEXT,
    last_notification_attempt_at TEXT,
    PRIMARY KEY (term, subject, catalog_number, section)
);

-- Append-only history. One row per successful, validated fetch. Never updated or
-- deleted, and never written for a failed/malformed fetch.
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term TEXT NOT NULL,
    subject TEXT NOT NULL,
    catalog_number TEXT NOT NULL,
    section TEXT NOT NULL,
    seats_available INTEGER NOT NULL,
    seats_capacity INTEGER NOT NULL,
    waitlist_available INTEGER,
    waitlist_capacity INTEGER,
    status TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_observations_section
    ON observations (term, subject, catalog_number, section, observed_at);

-- Fetch-orchestration bookkeeping per course (one HTTP call covers every section of
-- it). Never holds seat/waitlist data and is never a stand-in for a section row - it
-- exists so a course-wide watch's failures can be counted and its circuit breaker
-- tripped before any real section under it has ever been successfully observed.
CREATE TABLE IF NOT EXISTS course_fetch_health (
    term TEXT NOT NULL,
    subject TEXT NOT NULL,
    catalog_number TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    next_retry_at TEXT,
    last_error TEXT,
    PRIMARY KEY (term, subject, catalog_number)
);
