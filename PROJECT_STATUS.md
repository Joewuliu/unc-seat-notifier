# Project Status

Last updated: 2026-08-06 (mid-session, after adding `test-email`). Facts below were
verified directly (pytest/ruff run, git status/log, file listing) at time of writing,
not recalled from memory.

## 1. Current architecture and implemented commands

Layered, domain-logic-separate-from-I/O design per CLAUDE.md:

```
src/unc_notifier/
  models.py            # pure domain dataclasses (CourseKey, SectionKey, WatchEntry,
                        # SeatSnapshot, SectionState, CourseFetchHealth, Decision,
                        # NotificationEvent, WaitlistPolicy)
  diff.py               # pure state machine: evaluate() decides should_attempt_notify,
                        # transitions, cooldown, pending-notification retry bookkeeping.
                        # No I/O, no wall-clock reads.
  poller.py              # orchestration: dedup courses, fetch, diff, persist, notify.
                        # Course-level fetch-health circuit breaker + backoff.
  config.py               # .env loading. Three loaders (see decisions below).
  logging_setup.py         # JSON-lines structured logging to file.
  cli.py                    # argparse entrypoint, 4 subcommands (below).
  __main__.py                # enables `python -m unc_notifier ...`
  scraper/
    base.py                   # abstract Scraper + FetchError
    fake.py                     # fixture-backed fake (scriptable, used by all
                                 # non-network tests)
    unc.py                       # REAL scraper: UncClassSearchScraper (requests.Session
                                  # + CSRF flow against UNC Tiled Search)
    tiled_parser.py                # pure HTML parsing (BeautifulSoup4), no network
  notifier/
    base.py, smtp.py, fake.py, console.py   # Notifier + SmtpNotifier (real) +
                                             # FakeNotifier (test) + ConsoleNotifier (real,
                                             # prints instead of emailing)
  state/
    repository.py, schema.sql   # SQLite persistence: section_state, observations
                                 # (append-only), course_fetch_health
```

**CLI commands (all wired, all tested):**
- `poll` — real end-to-end cycle: fetch every watched course (from `watches.yaml`),
  diff, persist, email via `SmtpNotifier`. Uses full `AppConfig` (`load_config`).
- `scrape-once --term --subject --catalog-number [--env-file]` — read-only smoke test
  of the real scraper for one course. No `Repository`/`SmtpNotifier` ever constructed.
  Uses `ScraperConfig` (`load_scraper_config`) — only needs `CONTACT_EMAIL`.
- `poll-once-dry-run --term --subject --catalog-number --section --db-path [--env-file]`
  — exercises the real scraper + real `diff.evaluate()` + real `Repository` for one
  section, printing every step (fetched state, previous state, transition, decision,
  would-notify, final state). `ConsoleNotifier` instead of SMTP. `--db-path` is
  `required=True` in argparse — there is no default, so it structurally cannot touch
  the production database. Also uses `ScraperConfig`.
- `test-email [--env-file]` — sends exactly one email, subject
  `[UNC Seat Notifier] Test Email`, via the real `SmtpNotifier.send_test_email()`.
  Uses `SmtpConfig` (`load_smtp_config`) — only needs `SMTP_HOST`/`PORT`/`FROM`/`TO`
  (+ optional `SMTP_USERNAME`/`PASSWORD`/`USE_TLS`). Never reads `CONTACT_EMAIL` or
  `watches.yaml`; never constructs `UncClassSearchScraper`, `Repository`, `Poller`, or
  a SQLite connection. Error output is passed through `_redact_secrets()` so
  `SMTP_USERNAME`/`SMTP_PASSWORD` can never reach stdout/stderr, even indirectly via an
  exception's string representation.

## 2. Key design decisions

**Tiled Search endpoint + CSRF/session flow.** The real data source is
`https://reports.unc.edu/class-search/tiled/`, not the originally-assumed JSON API
(SPEC.md's assumption was wrong; confirmed via captured fixtures). Standard Django
CSRF double-submit: one `requests.Session` GETs the page once to obtain
`csrfmiddlewaretoken` + the session cookie, then POSTs `term`/`subject`/`catalog_number`.
`UncClassSearchScraper` primes the session at most once per instance lifetime and
reuses it for every subsequent `fetch_course()` call — since the CLI is
single-shot-per-process, this gives "once per poll run" for free. Tiled Search was
chosen over Standard/Advanced Search specifically because it's the only view that
exposes seat *capacity* (`N/M available seats`), not just seats-available.

**Waitlist fields are nullable (`int | None`), not required ints.** Confirmed via
three real captured views (Standard, Advanced, Tiled search) that UNC's public search
never exposes waitlist data at all. `SeatSnapshot`, `SectionState`, and
`NotificationEvent` all carry `waitlist_available`/`waitlist_capacity: int | None`.
`SeatSnapshot.waitlist_status_unknown` is `True` whenever either is `None`.

**Unknown waitlist policy behavior.** `waitlist_active` (and thus
`people_on_waitlist`) evaluates to `False`/`0` when waitlist status is unknown —
so `WaitlistPolicy.SILENT`/`WARN` become no-ops against real data (every real
transition behaves like `ALERT`). This is a deliberate, explicit choice: unknown is
never silently treated as "confirmed no waitlist" in *user-facing text* —
`SmtpNotifier`/`ConsoleNotifier` print `Waitlist: unknown`, never a fabricated `0/0
occupied` — but it *is* treated as non-blocking for the purposes of deciding whether
to suppress/warn on a notification.

**Malformed HTML fails the whole fetch, never partial data.** `tiled_parser.py`
raises `FetchError` (not a partial result) for: missing `<main id="main">`; neither a
results summary nor a "No results found." jumbotron present; card count mismatch
against the page's own declared section count; distinct-course count mismatch against
the declared course count; an unparseable `<h2>` header (not exactly 3
whitespace-separated tokens); unparseable `N/M available seats` text. A legitimate
zero-results page (jumbotron containing "No results found.", confirmed to have zero
cards) returns `[]` — a valid success, not an error.

**Config loading is split by command, not shared.** Three independent loaders in
`config.py`, each reading only what its command needs and each raising `ConfigError`
if that (and only that) subset is missing/invalid: `load_config()` (full `AppConfig` —
`poll`), `load_scraper_config()` (`ScraperConfig`, just `CONTACT_EMAIL` — `scrape-once`,
`poll-once-dry-run`), `load_smtp_config()` (`SmtpConfig`, just `SMTP_*` — `test-email`).
None of the three call each other; each duplicates a small amount of validation logic
rather than sharing it, so that e.g. broken `SMTP_PORT` can never block `scrape-once`
and a missing `CONTACT_EMAIL` can never block `test-email`. `_load_env_values()` is the
one genuinely shared helper (just the `.env` + process-environment merge).

**`SmtpNotifier.notify()` and `.send_test_email()` share one `_send()` method** for
the actual connect/TLS/login/`send_message()` sequence, so `test-email` exercises the
exact same SMTP transport code path as real seat-alert emails — only the message
content (fixed subject/body vs. per-event content) differs.

Other decisions carried from earlier in the build (still in effect, not restated in
detail here): `last_notification_attempt_at` is stamped by the poller immediately
before calling `notifier.notify()`, never by the pure `diff.evaluate()`; course-level
`course_fetch_health` table (separate from per-section `consecutive_failures`) so a
course-wide watch's fetch failures are tracked even before any section has ever been
observed; SQLite via stdlib `sqlite3`, not an ORM.

## 3. Fixture files and what each proves

| File | Proves |
|---|---|
| `comp550_available.json`, `comp550_full.json`, `math231_waitlisted.json` | Hand-authored fixtures for `FakeScraper`-driven tests (diff/poller/repository logic) — not real UNC data, our own normalized shape. |
| `unc_class_search_comp210.html` | Real Standard Search response. Established the table/rowspan structure and confirmed the endpoint is HTML, not JSON — historical/reference now, not exercised by current parser code (Standard Search isn't scraped). |
| `unc_class_search_advanced_comp210.html` | Real Advanced Search response. Confirmed it uses the *same* table structure as Standard (no capacity, no waitlist) — ruled out as a better data source than Tiled. Not exercised by current parser code. |
| `unc_class_search_tiled_comp210.html` | Real Tiled Search, 2 sections. Primary fixture for `tiled_parser` happy-path tests and `test_unc_scraper.py`'s mocked end-to-end flow. |
| `unc_class_search_tiled_subject_all.html` | Real Tiled Search, unfiltered by catalog number: 47 courses / 324 sections. Proves zero-available rendering (`0/<capacity>`, no special "Closed" text), alphanumeric catalog numbers (`89H`, `283H`), and result-count validation at real scale. |
| `unc_class_search_tiled_zero_results.html` | Real Tiled Search with no matches. Proves the "No results found." jumbotron path returns `[]`, not `FetchError`. |

(`test-email` needed no new fixtures — its tests mock `smtplib.SMTP` directly, same
pattern as the existing `SmtpNotifier` tests.)

## 4. Test count and last run results

```
pytest -q  → 134 passed
ruff check .  → All checks passed
```

Per-file breakdown (`grep -c '^def test_'`): test_cli.py 24, test_config.py 17,
test_console_notifier.py 3, test_diff.py 15, test_logging_setup.py 2, test_poller.py 27,
test_repository.py 13, test_scraper_fake.py 5, test_smtp_notifier.py 6,
test_tiled_parser.py 13, test_unc_scraper.py 9. Sum = 134.

## 5. Work completed but not committed

Last commit: `171b0cb Add safe end-to-end polling dry run` (this includes the
`poll-once-dry-run` command, `ConsoleNotifier`, and the previous revision of this
status file — that work is now committed, unlike what an earlier version of this
document said). Uncommitted since then (the `test-email` command, this session):

```
 M README.md
 M src/unc_notifier/cli.py
 M src/unc_notifier/config.py
 M src/unc_notifier/notifier/smtp.py
 M tests/test_cli.py
 M tests/test_config.py
 M tests/test_smtp_notifier.py
```

(Plus this file.) Nothing has been committed at the user's explicit direction
throughout this session.

## 6. Next planned phase

Not yet decided/started. No later phase (web app, signup, scheduling/deployment
automation) has been scoped or begun — explicitly deferred each time it came up. Open
items, not yet acted on: whether to pursue a dedicated fixture isolating a single
full/closed section card (beyond what `unc_class_search_tiled_subject_all.html`
already covers), and whether the user wants to move to committing this work or
continue adding diagnostic tooling before touching the real `poll` cron path in
production.

## 7. Commands to verify locally

```bash
./.venv/Scripts/python.exe -m pytest -q
./.venv/Scripts/python.exe -m ruff check .

# Read-only live smoke test (only needs CONTACT_EMAIL in .env):
./.venv/Scripts/python.exe -m unc_notifier scrape-once --term "2026 Fall" --subject COMP --catalog-number 210

# Full dry-run against a scratch DB (never touches production data/unc_notifier.db):
./.venv/Scripts/python.exe -m unc_notifier poll-once-dry-run \
  --term "2026 Fall" --subject COMP --catalog-number 210 --section 001 \
  --db-path .tmp/dry-run-test.db

# SMTP configuration test (only needs SMTP_* in .env, sends one real email):
./.venv/Scripts/python.exe -m unc_notifier test-email
```

## 8. Known limitations / technical debt

- **No fixture for a genuinely full/closed section's exact rendering beyond the
  `0/<capacity>` number** confirmed in `unc_class_search_tiled_subject_all.html` — that
  fixture already contains real full sections, so this is largely covered, but no
  fixture isolates a single closed-section card the way `unc_class_search_tiled_comp210.html`
  does for the open case.
- **`poll-once-dry-run` duplicates `Poller._attempt_notify`'s success/failure state
  resolution logic** (in `cli.py::_attempt_console_notify`) rather than calling the
  real `Poller` internals, because `Poller.run()` doesn't expose per-section decision
  details needed for the step-by-step printing this command does, and double-fetching
  to get both a preview and the real result was rejected as worse (risk of
  fetch-to-fetch data drift). This is a deliberate, disclosed tradeoff, not an oversight
  — flagged here so the two code paths are checked together if either changes.
- **Cross-listed courses** ("Same As" field, confirmed present in the real HTML) are
  parsed but not modeled — if a course is cross-listed under two different
  `(subject, catalog_number)` pairs, watching both would poll/store them as unrelated
  entries even though they may share the same physical seats. Documented, not solved.
- **CSRF token is never refreshed mid-run.** If a long poll run's token expires or is
  invalidated server-side, `UncClassSearchScraper` will surface subsequent POSTs as
  `FetchError` (correct failure handling) but does not retry with a fresh GET. No
  evidence yet that this occurs in practice.
- **Wire-level request requirements aren't fully confirmed** — `Referer` header is
  sent proactively as a defensive guess (Django CSRF sometimes requires it on HTTPS)
  but this hasn't been empirically forced to fail without it.
- **`_redact_secrets()` is a simple substring replace**, applied only within
  `_run_test_email`'s own exception-message formatting. It protects the one place a
  secret could plausibly leak in this command (an SMTP error message echoing input),
  but it is not a general-purpose logging filter — if `test-email` or `SmtpNotifier`
  gains new output paths later, each would need the same treatment explicitly.
- `.env` currently open in the user's editor is presumed to hold real local
  configuration; not inspected or read as part of this status file.
