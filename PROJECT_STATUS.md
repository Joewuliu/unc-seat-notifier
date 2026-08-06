# Project Status

Last updated: 2026-08-06 (mid-session, after adding `poll-once-live`). Facts below
were verified directly (pytest/ruff run, git status/log, file listing) at time of
writing, not recalled from memory.

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
  poller.py              # orchestration. Poller.apply_snapshot() is the one place
                        # notification-resolution logic lives (diff -> notify-attempt
                        # -> persist), returning a SectionPollResult(decision,
                        # final_state, notified) - shared by the real poll cycle
                        # (via _poll_course) and poll-once-live. Course-level
                        # fetch-health circuit breaker + backoff (_poll_course only).
  config.py               # .env loading. Three independent loaders (see decisions).
  logging_setup.py         # JSON-lines structured logging to file.
  cli.py                    # argparse entrypoint, 5 subcommands (below).
  __main__.py                # enables `python -m unc_notifier ...`
  scraper/
    base.py                   # abstract Scraper + FetchError
    fake.py                     # fixture-backed fake (scriptable, used by all
                                 # non-network tests)
    unc.py                       # REAL scraper: UncClassSearchScraper (requests.Session
                                  # + CSRF flow against UNC Tiled Search)
    tiled_parser.py                # pure HTML parsing (BeautifulSoup4), no network
  notifier/
    base.py, smtp.py, fake.py, console.py   # Notifier + SmtpNotifier (real, has both
                                             # notify() and send_test_email()) +
                                             # FakeNotifier (test) + ConsoleNotifier
                                             # (real, prints instead of emailing)
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
  section, printing every step. `ConsoleNotifier` instead of SMTP. `--db-path` is
  `required=True` — no default, so it structurally cannot touch the production
  database. Uses `ScraperConfig`.
- `test-email [--env-file]` — sends exactly one email, subject
  `[UNC Seat Notifier] Test Email`, via the real `SmtpNotifier.send_test_email()`.
  Uses `SmtpConfig` (`load_smtp_config`) — only needs `SMTP_HOST`/`PORT`/`FROM`/`TO`.
  Never reads `CONTACT_EMAIL`/`watches.yaml`; never constructs `UncClassSearchScraper`,
  `Repository`, `Poller`, or a SQLite connection. Errors pass through
  `_redact_secrets()`.
- `poll-once-live --term --subject --catalog-number --section --db-path [--env-file]`
  — the real end-to-end poll for one section: real `UncClassSearchScraper`, real
  `Repository`, real `Poller.apply_snapshot()`, real `SmtpNotifier`. Requires
  `--db-path` (no default) **and** `--confirm-send-email` — without the latter the
  command refuses to run before fetching anything or constructing `SmtpNotifier`.
  `--seed-closed-state` (gated by its own `--confirm-seed`) is a separate,
  network-free mode that writes a synthetic closed `SectionState` for the requested
  section so a later live run can demonstrate a real closed→open email. On SMTP
  failure the fetched seat state is still persisted, the notification stays
  pending/retryable, and the command still exits `0` (matching production `poll`'s
  treatment of notify failures as non-fatal/retryable). Attaches a `NullHandler` to
  the `unc_notifier` logger so `Poller`'s internal error logging (which includes raw
  notifier exception text) can never leak to the console via Python's default
  "no handler configured" stderr fallback; fetch-error output is also passed through
  `_redact_secrets()`.

## 2. Key design decisions

**`Poller.apply_snapshot()` is the one shared home for notification-resolution
logic — added specifically to stop re-duplicating it.** `poll-once-dry-run` (previous
session) had to hand-roll its own notify-attempt/persist logic because `Poller` only
exposed `run()` (aggregate `PollSummary`, no per-section detail) and private
`_apply_snapshot()`/`_attempt_notify()`. Before building `poll-once-live`, this was
flagged to the user as tech debt and a small refactor was proposed and **approved**
before implementation: `_apply_snapshot` → public `apply_snapshot`, returning a new
`SectionPollResult(decision, final_state, notified)` instead of `None`;
`_attempt_notify` now returns `tuple[SectionState, bool]` (adds the success flag).
Single call site each (`_poll_course`, both private) — `poll`'s behavior and every
existing test were unaffected (verified via full suite before/after). `poll-once-live`
calls `apply_snapshot()` directly after fetching/reading prior state itself, getting
the exact same decision/persistence/SMTP-success-gated `last_notified_at` guarantees
`poll` gets, with no re-implementation. `poll-once-dry-run` was deliberately *not*
retrofitted to use it (out of scope for this change; still uses its own
`_attempt_console_notify` — see known limitations).

**Tiled Search endpoint + CSRF/session flow.** The real data source is
`https://reports.unc.edu/class-search/tiled/`, not the originally-assumed JSON API
(SPEC.md's assumption was wrong; confirmed via captured fixtures). Standard Django
CSRF double-submit: one `requests.Session` GETs the page once to obtain
`csrfmiddlewaretoken` + the session cookie, then POSTs `term`/`subject`/`catalog_number`.
`UncClassSearchScraper` primes the session at most once per instance lifetime and
reuses it for every subsequent `fetch_course()` call. Tiled Search was chosen over
Standard/Advanced Search specifically because it's the only view that exposes seat
*capacity* (`N/M available seats`), not just seats-available.

**Waitlist fields are nullable (`int | None`), not required ints.** Confirmed via
three real captured views (Standard, Advanced, Tiled search) that UNC's public search
never exposes waitlist data at all. `SeatSnapshot`, `SectionState`, and
`NotificationEvent` all carry `waitlist_available`/`waitlist_capacity: int | None`.

**Unknown waitlist policy behavior.** `waitlist_active` evaluates to `False` when
waitlist status is unknown — `WaitlistPolicy.SILENT`/`WARN` become no-ops against real
data (every real transition behaves like `ALERT`). User-facing text always says
"unknown," never a fabricated "0/0 occupied."

**Malformed HTML fails the whole fetch, never partial data.** `tiled_parser.py`
raises `FetchError` for any structural mismatch (missing `<main>`, missing both the
results summary and "No results found.", card/course count mismatches, unparseable
header/seat text). A legitimate zero-results page returns `[]` — success, not error.

**Config loading is split by command, not shared.** Three independent loaders in
`config.py`: `load_config()` (full `AppConfig` — `poll`), `load_scraper_config()`
(`ScraperConfig`, just `CONTACT_EMAIL` — `scrape-once`, `poll-once-dry-run`),
`load_smtp_config()` (`SmtpConfig`, just `SMTP_*` — `test-email`).
`poll-once-live` is the first command to need **both** scraper and SMTP config
simultaneously — it calls `load_scraper_config()` and `load_smtp_config()`
independently (no new combined loader), consistent with "each command reads only
what it needs."

**`SmtpNotifier.notify()` and `.send_test_email()` share one `_send()` method** for
the connect/TLS/login/`send_message()` sequence.

Other decisions carried from earlier in the build (still in effect, not restated in
detail here): `last_notification_attempt_at` is stamped by the poller immediately
before calling `notifier.notify()`, never by the pure `diff.evaluate()`; course-level
`course_fetch_health` table separate from per-section `consecutive_failures`; SQLite
via stdlib `sqlite3`, not an ORM.

## 3. Fixture files and what each proves

| File | Proves |
|---|---|
| `comp550_available.json`, `comp550_full.json`, `math231_waitlisted.json` | Hand-authored fixtures for `FakeScraper`-driven tests (diff/poller/repository logic) — not real UNC data, our own normalized shape. |
| `unc_class_search_comp210.html` | Real Standard Search response. Established the table/rowspan structure and confirmed the endpoint is HTML, not JSON — historical/reference now, not exercised by current parser code. |
| `unc_class_search_advanced_comp210.html` | Real Advanced Search response. Confirmed it uses the *same* table structure as Standard (no capacity, no waitlist) — ruled out as a better data source than Tiled. Not exercised by current parser code. |
| `unc_class_search_tiled_comp210.html` | Real Tiled Search, 2 sections. Primary fixture for `tiled_parser` happy-path tests and `test_unc_scraper.py`'s mocked end-to-end flow. |
| `unc_class_search_tiled_subject_all.html` | Real Tiled Search, unfiltered by catalog number: 47 courses / 324 sections. Proves zero-available rendering, alphanumeric catalog numbers (`89H`, `283H`), and result-count validation at real scale. |
| `unc_class_search_tiled_zero_results.html` | Real Tiled Search with no matches. Proves the "No results found." jumbotron path returns `[]`, not `FetchError`. |

(`test-email` and `poll-once-live` needed no new HTML fixtures — their tests mock
`smtplib.SMTP` and/or script `UncClassSearchScraper` directly, same pattern as
existing tests.)

## 4. Test count and last run results

```
pytest -q  → 146 passed
ruff check .  → All checks passed
```

Per-file breakdown (`grep -c '^def test_'`): test_cli.py 36, test_config.py 17,
test_console_notifier.py 3, test_diff.py 15, test_logging_setup.py 2, test_poller.py 27,
test_repository.py 13, test_scraper_fake.py 5, test_smtp_notifier.py 6,
test_tiled_parser.py 13, test_unc_scraper.py 9. Sum = 146.

Also manually verified against the real UNC site / real argparse wiring this session:
`poll-once-live --help`; `--seed-closed-state --confirm-seed` (writes real synthetic
state, no network call, confirmed via printed output); the no-`--confirm-send-email`
refusal path (confirmed no fetch attempted). A real `--confirm-send-email` live run
was deliberately **not** executed, since it would send a real email using the user's
real SMTP credentials from their local `.env` — left for the user to trigger
themselves.

## 5. Work completed but not committed

Last commit: `57f8b6a Add controlled SMTP test command` (includes `test-email`,
`load_smtp_config`, `SmtpNotifier.send_test_email()` — that work is now committed).
Uncommitted since then (the `poll-once-live` command + `Poller` refactor, this
session):

```
 M README.md
 M src/unc_notifier/cli.py
 M src/unc_notifier/poller.py
 M tests/test_cli.py
```

(Plus this file.) Nothing has been committed at the user's explicit direction
throughout this session.

## 6. Next planned phase

Not yet decided/started. No later phase (web app, signup, scheduling/deployment
automation) has been scoped or begun — explicitly deferred each time it came up, and
`poll-once-live` explicitly does not add scheduling/cron/Task Scheduler support per
this session's instructions. Open items, not yet acted on: whether to retrofit
`poll-once-dry-run` onto `Poller.apply_snapshot()` now that it exists (would retire
`_attempt_console_notify`'s duplicated logic — see known limitations); whether the
user wants to trigger a real `--confirm-send-email` live run themselves to see an
actual delivered email; whether to move to committing this work.

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

# Real live poll for one section (needs CONTACT_EMAIL + SMTP_* in .env; can send a
# real email - see README.md's "Safe test sequence" for the seed-first walkthrough):
./.venv/Scripts/python.exe -m unc_notifier poll-once-live \
  --term "2026 Fall" --subject COMP --catalog-number 210 --section 001 \
  --db-path .tmp/live-test.db --seed-closed-state --confirm-seed
./.venv/Scripts/python.exe -m unc_notifier poll-once-live \
  --term "2026 Fall" --subject COMP --catalog-number 210 --section 001 \
  --db-path .tmp/live-test.db --confirm-send-email
```

## 8. Known limitations / technical debt

- **`poll-once-dry-run` still duplicates notify-resolution logic** (`cli.py::
  _attempt_console_notify`), even though `Poller.apply_snapshot()` now exists and
  could replace it. Retrofitting was deliberately left out of this session's scope
  (only `poll-once-live` was asked for) — flagged here as the natural next cleanup.
- **No fixture for a genuinely full/closed section's exact rendering beyond the
  `0/<capacity>` number** confirmed in `unc_class_search_tiled_subject_all.html`.
- **Cross-listed courses** ("Same As" field, confirmed present in the real HTML) are
  parsed but not modeled — watching both listings of a cross-listed course would
  poll/store them as unrelated entries even though they may share physical seats.
- **CSRF token is never refreshed mid-run.** A long run's token expiring would surface
  as `FetchError` (correct handling) but without a retry-with-fresh-GET. No evidence
  this occurs in practice.
- **Wire-level request requirements aren't fully confirmed** — `Referer` header is
  sent proactively as a defensive guess, not empirically verified as required.
- **`_redact_secrets()` is a simple substring replace**, applied only at each
  command's own print sites (not a general logging filter). `poll-once-live`
  additionally attaches a `NullHandler` to suppress `Poller`'s internal logger output
  from leaking via Python's default stderr fallback - see design decisions above -
  but this pairing (explicit redaction + logger suppression) would need to be
  reapplied deliberately if new output paths are added to either command.
- **`poll-once-live`'s seeded `seats_capacity` is a fixed placeholder (`1`)**, not a
  real captured capacity — fine for demonstrating a closed→open transition, but the
  seeded row's capacity number itself is synthetic and shouldn't be read as real data.
- `.env` currently open in the user's editor is presumed to hold real local
  configuration; not inspected or read as part of this status file.
