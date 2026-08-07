# Project Status

Last updated: 2026-08-07 (after building the Task Scheduler registration workflow -
`register_task.ps1`/`remove_task.ps1`/`verify_task.ps1` - see §2/§6). Facts below
were verified directly (pytest/ruff run, git status/log, real manual invocations,
file listing) at time of writing, not recalled from memory.

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
                        # Unchanged this session - reused as-is for production work.
  config.py               # .env loading. Three independent loaders (see decisions).
                          # Relative DB_PATH/LOG_PATH/WATCHES_PATH/LOCK_PATH resolve
                          # against the .env file's own directory, not the CWD.
  lock.py                   # single_instance_lock(): stdlib msvcrt-based cross-
                            # process lock so an overlapping scheduled poll refuses
                            # to start. New this session.
  logging_setup.py           # JSON-lines structured logging to file.
  cli.py                       # argparse entrypoint, 6 subcommands (below).
  __main__.py                    # enables `python -m unc_notifier ...`
  scraper/
    base.py                       # abstract Scraper + FetchError
    fake.py                         # fixture-backed fake (scriptable, used by all
                                     # non-network tests)
    unc.py                           # REAL scraper: UncClassSearchScraper (requests.Session
                                      # + CSRF flow against UNC Tiled Search). 30s
                                      # connect/read timeout on every request.
    tiled_parser.py                    # pure HTML parsing (BeautifulSoup4), no network.
                                        # parse_tiled_search() now takes a required
                                        # course_key and filters cards down to an
                                        # exact (term, subject, catalog_number)
                                        # match - see design decisions.
  notifier/
    base.py, smtp.py, fake.py, console.py   # Notifier + SmtpNotifier (real, has both
                                             # notify() and send_test_email(), 30s
                                             # connect timeout) + FakeNotifier (test)
                                             # + ConsoleNotifier (real, prints instead
                                             # of emailing)
  state/
    repository.py, schema.sql   # SQLite persistence: section_state, observations
                                 # (append-only), course_fetch_health
scripts/
  run_poll.ps1                    # Windows Task Scheduler launcher.
  register_task.ps1                # New this session. Previews (default) or, with
                                    # -ConfirmRegister, registers the scheduled task
                                    # via the ScheduledTasks PowerShell module.
  remove_task.ps1                   # New this session. Previews (default) or, with
                                     # -ConfirmRemove, unregisters it.
  verify_task.ps1                    # New this session. Read-only status report
                                      # (state, last run/result, next run, logs).
  show_task_scheduler_command.ps1      # Superseded by register_task.ps1 - kept only
                                        # as a historical schtasks.exe-based reference.
```

**CLI commands (all wired, all tested):**
- `poll` — real end-to-end cycle: fetch every watched course (from `watches.yaml`),
  diff, persist, email via `SmtpNotifier`. Uses full `AppConfig` (`load_config`).
  **This session:** prints resolved (non-secret) config/db/log/watches/lock paths
  and watched-entry count at startup; the whole cycle now runs inside
  `single_instance_lock(config.lock_path)` - a second overlapping invocation prints
  a clear message and exits `0` (not an error) instead of racing the first one;
  logs `run_start`/`run_end exit_status=N` in addition to the existing
  `poll_complete` summary line. Behavior for a normal, non-overlapping run is
  unchanged (all pre-existing `poll` tests still pass unmodified).
- `production-check [--env-file]` — **new this session.** Read-only validation that
  everything `poll` needs is present and structurally valid: config parses,
  watches file exists and parses, db/log/lock directories are writable (their
  nearest *existing* ancestor, since `poll` creates the target directory itself on
  first run - this command never creates it), SMTP settings are structurally sane
  (no connection attempted), `CONTACT_EMAIL` present. Also prints
  `sys.executable`/version and whether a venv is active. Exits `0` only if every
  check passes. Never scrapes UNC, sends email, or creates/modifies the production
  database or `watches.yaml` - the only filesystem write anywhere in this command
  is a temp probe file, created and deleted immediately, used to test directory
  writability. Never prints `SMTP_USERNAME`/`SMTP_PASSWORD`.
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
  pending/retryable, and the command still exits `0`. Attaches a `NullHandler` to
  the `unc_notifier` logger so `Poller`'s internal error logging (raw notifier
  exception text) can never leak via Python's default stderr fallback; fetch-error
  output is also passed through `_redact_secrets()`.

## 2. Key design decisions

**Task Scheduler registration workflow (this session).** Built
`scripts/register_task.ps1`/`remove_task.ps1`/`verify_task.ps1` to replace
`show_task_scheduler_command.ps1` as the canonical registration path (that older
script is left in place, undocumented as the primary path, purely historical).
Two decisions worth recording:

- **Uses the `ScheduledTasks` PowerShell module (`Register-ScheduledTask` and
  friends), not `schtasks.exe`.** Several required settings - execution time limit,
  restart count/interval, "start as soon as possible after a missed start", running
  on battery - aren't expressible through `schtasks.exe`'s `/Create` flags, but map
  directly onto `New-ScheduledTaskSettingsSet` parameters. This was close to a
  forced move given the requested settings list, not really a judgment call between
  comparable options.
- **`register_task.ps1`'s preview output is generated from the literal
  action/trigger/settings/principal objects that would be passed to
  `Register-ScheduledTask`**, not a separate hand-written description - so what gets
  previewed can't drift out of sync with what actually gets registered. Those
  constructor cmdlets (`New-ScheduledTaskAction` etc.) have no side effects on their
  own; only the final `Register-ScheduledTask` call (gated behind `-ConfirmRegister`)
  touches the real Task Scheduler store. This also makes the preview path fully
  testable without ever registering anything for real.

Confirmed in this environment that registering (or removing) a real scheduled task
under the recommended `SYSTEM` account requires an elevated ("Run as
Administrator") PowerShell session - a non-elevated `Register-ScheduledTask` call
fails with "Access is denied." All three new scripts, and their tests, account for
this: registration/removal were never exercised for real (see §4/§6).

**Course-card filtering bug fix (prior session).** A real production `poll` run for
MATH 231-001 raised a fetch failure: UNC's real Tiled Search, when searched with
`catalog_number=231`, also returned a card for the related-but-distinct `MATH 231L`
(a lab section with its own catalog number). The old `parse_tiled_search()` had no
concept of "the course I actually asked for" - it returned every card on the page,
and the *caller* (`Poller._validate_snapshots()`) rejected the whole fetch the
moment it saw a section whose course didn't match what was requested. Fixed by
giving `parse_tiled_search(html, *, observed_at, course_key)` a required
`course_key` parameter: it still parses and structurally validates **every** card
on the page (including the declared "Found N course(s), M section(s))" summary
counts) *before* filtering - a malformed card or a declared-count mismatch fails the
whole fetch regardless of which course it belongs to, since that signals something
is wrong with the page itself - and only *after* that full-page validation does it
filter down to cards whose `(term, subject, catalog_number)` exactly match
`course_key`. A page with no exact match (only related cards) returns `[]`, a valid
empty result, not an error. `UncClassSearchScraper.fetch_course()` passes its own
`course_key` straight through. `Poller._validate_snapshots()`'s existing
course-identity check was deliberately left in place unchanged (defense-in-depth,
not weakened) even though the scraper now guarantees it can never trigger in
practice. Regression-tested with two new hand-crafted fixtures
(`unc_class_search_tiled_math231_with_231l.html`,
`_math231_only_related.html`) mirroring the exact real scenario, plus reused the
existing real `unc_class_search_tiled_subject_all.html` fixture's genuine `COMP 89`
vs `89H` and `283` vs `283H` pairs as an additional real-data regression case.

**Production-readiness work (prior session) — proposed via `AskUserQuestion` and
approved before implementation:**

- **Single-instance lock: stdlib `msvcrt` file lock, not a third-party library or a
  hand-rolled PID file.** `lock.py`'s `single_instance_lock()` opens/creates a lock
  file and takes a non-blocking exclusive byte-range lock via `msvcrt.locking()`.
  Zero new dependencies. Chosen specifically because the lock is tied to the OS file
  handle: if the holding process crashes or is killed, Windows releases the lock the
  moment it tears down that process's handles - there is no separate "is this lock
  stale" check to get wrong, unlike a PID file. Windows-only, matching this whole
  feature's scope (PowerShell launcher, Task Scheduler). `poll` wraps its entire
  cycle in this lock; on `LockUnavailable` it prints a clear message and exits `0`
  (an overlapping run is expected/benign under a 5-minute schedule, not a failure).
- **Path resolution: relative `DB_PATH`/`LOG_PATH`/`WATCHES_PATH`/`LOCK_PATH` in
  `.env` resolve against the `.env` file's own directory, not the process's CWD.**
  `config.py`'s new `_path_base_dir()`/`_resolve_configured_path()` implement this;
  it only changes behavior when `--env-file` is passed explicitly (which the
  production launcher always does, using an absolute path computed from its own
  script location) - with no `--env-file` at all, paths stay CWD-relative,
  preserving today's interactive/dev behavior. This makes Task Scheduler's "Start
  in" directory irrelevant to where `poll` reads/writes its files, without requiring
  every value in `.env.example` to become an absolute path.

Other decisions carried from earlier sessions (still in effect, not restated in
detail here):

**`Poller.apply_snapshot()` is the one shared home for notification-resolution
logic.** `_apply_snapshot`/`_attempt_notify` were made public/return-value-bearing
(`SectionPollResult`) specifically so `poll-once-live` doesn't re-duplicate
notify/persist logic; `poll-once-dry-run` still has its own parallel
`_attempt_console_notify` (known limitation, unchanged this session).

**Tiled Search endpoint + CSRF/session flow**, **nullable waitlist fields**,
**unknown-waitlist-policy behavior**, **malformed HTML fails the whole fetch**,
**config loading split by command**, **`SmtpNotifier` shares one `_send()`
method**, **`last_notification_attempt_at` stamped by the poller, never by pure
`diff.evaluate()`**, **course-level `course_fetch_health` separate from per-section
`consecutive_failures`**, **SQLite via stdlib `sqlite3`, not an ORM** — see prior
session history if full detail is needed; none of these changed this session.

## 3. Fixture files and what each proves

| File | Proves |
|---|---|
| `comp550_available.json`, `comp550_full.json`, `math231_waitlisted.json` | Hand-authored fixtures for `FakeScraper`-driven tests (diff/poller/repository logic) — not real UNC data, our own normalized shape. |
| `unc_class_search_comp210.html` | Real Standard Search response. Historical/reference — not exercised by current parser code. |
| `unc_class_search_advanced_comp210.html` | Real Advanced Search response. Historical/reference — not exercised by current parser code. |
| `unc_class_search_tiled_comp210.html` | Real Tiled Search, 2 sections. Primary fixture for `tiled_parser` happy-path tests and `test_unc_scraper.py`'s mocked end-to-end flow. |
| `unc_class_search_tiled_subject_all.html` | Real Tiled Search, unfiltered by catalog number: 47 courses / 324 sections. Proves zero-available rendering, result-count validation at real scale, **and** (this session) exact-vs-related catalog-number filtering using its genuine `COMP 89`/`89H` and `283`/`283H` pairs. |
| `unc_class_search_tiled_zero_results.html` | Real Tiled Search with no matches. Proves the "No results found." jumbotron path returns `[]`, not `FetchError`. |
| `unc_class_search_tiled_math231_with_231l.html` | **New this session.** Hand-crafted (real card markup, synthetic content) - a MATH 231 card alongside the related MATH 231L card, mirroring the exact scenario that broke production. Proves exact-course filtering keeps 231, drops 231L, and vice versa. |
| `unc_class_search_tiled_math231_only_related.html` | **New this session.** A structurally valid page containing only the related MATH 231L card (no exact 231 match at all) - proves that case returns `[]`, not `FetchError`. |

## 4. Test count and last run results

```
pytest -q  → 187 passed
ruff check .  → All checks passed
```

Per-file breakdown (`grep -c '^def test_'`): test_cli.py 46, test_config.py 21,
test_console_notifier.py 3, test_diff.py 15, test_lock.py 5, test_logging_setup.py 2,
test_poller.py 27, test_repository.py 13, test_run_poll_launcher.py 5,
test_scraper_fake.py 5, test_smtp_notifier.py 6, test_task_scheduler_scripts.py 11,
test_tiled_parser.py 19, test_unc_scraper.py 9. Sum = 187.

`test_run_poll_launcher.py` and (new this session) `test_task_scheduler_scripts.py`
both run the *real* PowerShell scripts via `powershell.exe` against synthetic repo
skeletons under `tmp_path` (never this checkout's real `.env`/`watches.yaml`/venv) -
`pytest.mark.skipif` if `powershell.exe`/`pwsh` isn't on `PATH`. `test_lock.py`
exercises the real Windows `msvcrt` file lock directly, including a
simulated-crash scenario.

**`test_task_scheduler_scripts.py` never registers or removes a real scheduled
task** - confirmed in this environment that doing so under the recommended
`SYSTEM` account requires an elevated PowerShell session
(`Register-ScheduledTask` fails with "Access is denied" otherwise), so every test
exercises only `register_task.ps1`'s/`remove_task.ps1`'s preview paths (which never
call `Register-ScheduledTask`/`Unregister-ScheduledTask`) plus the read-only
"task doesn't exist" paths, always against a random `-TaskName` so nothing can
collide with a real "UNC Seat Notifier" task on this machine. The "task exists,
gets removed for real" path is covered only by a source-level regression check
(the `-ConfirmRemove` guard appears before the `Unregister-ScheduledTask` call) -
see known limitations.

Also manually verified for real this session (all read-only/safe, via the actual
`scripts\register_task.ps1`/`remove_task.ps1`/`verify_task.ps1` against this real
checkout): preview mode's full settings table (see §7 output below); `verify_task.ps1`
and `remove_task.ps1` both correctly reporting "no task named 'UNC Seat Notifier'
exists" (it isn't registered yet). Real registration was **not** attempted - it
would need an elevated session and, per this session's instructions, requires the
user's separate go-ahead first (see §6).

## 5. Work completed but not committed

**Since the last status update, the user committed everything through the
`tiled_parser.py` course-filtering fix** (commit `aa82337 Prepare reliable Windows
production polling` - includes `poll-once-live`, all prior-session
production-readiness work, the MATH 231/231L fix, and their own real
`watches.yaml`). Uncommitted now, per the user's explicit "do not commit"
direction this session:

```
 M PROJECT_STATUS.md
 M README.md
?? scripts/register_task.ps1
?? scripts/remove_task.ps1
?? scripts/verify_task.ps1
?? tests/test_task_scheduler_scripts.py
```

## 6. Next planned phase

**Explicitly not done, and requires the user's separate approval before it
happens:** registering the actual Windows Task Scheduler task.
`scripts/register_task.ps1` (default, no flags) only *previews* every setting - it
never calls `Register-ScheduledTask` unless `-ConfirmRegister` is passed
explicitly, which has not happened. Real registration (and removal) additionally
requires an elevated ("Run as Administrator") PowerShell session, confirmed via a
real "Access is denied" error in this environment under a normal session. The
natural next step, when the user is ready: review the preview output (§7), open an
elevated PowerShell session, run `register_task.ps1 -ConfirmRegister`, then
`verify_task.ps1` to confirm it's really there.

Other open items, not yet acted on: whether to retrofit `poll-once-dry-run` onto
`Poller.apply_snapshot()`; whether to commit this session's registration-workflow
scripts.

## 7. Commands to verify locally

```bash
./.venv/Scripts/python.exe -m pytest -q
./.venv/Scripts/python.exe -m ruff check .

# Read-only production readiness check (no network, no email, no db writes):
./.venv/Scripts/python.exe -m unc_notifier production-check

# Launcher dry run - prints resolved paths, touches nothing:
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_poll.ps1 -DryRun

# Preview the Task Scheduler registration - prints every setting, registers nothing:
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/register_task.ps1

# Read-only status report (works before or after registration):
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify_task.ps1

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

- **A real `watches.yaml` exists and is now committed** (part of `aa82337`,
  including the user's real course watches) - it was a real scheduled `poll` run
  against it, watching MATH 231-001, that surfaced the course-filtering bug fixed
  in the prior session.
- **Exact-match filtering is per-request, not per-family** - watching MATH 231L
  specifically (e.g. to get its own seat alerts) requires its own separate
  `watches.yaml` entry with `catalog_number: "231L"`; there's no "watch 231 and
  everything related to it" mode, by design (matches "filter to the exact
  requested term/subject/catalog_number," not a fuzzy/prefix match).
- **`poll-once-dry-run` still duplicates notify-resolution logic** (`cli.py::
  _attempt_console_notify`) instead of using `Poller.apply_snapshot()` - unchanged,
  flagged in earlier sessions too.
- **The Task Scheduler task itself is not registered** - `Register-ScheduledTask`
  was never called this session (only previewed), per explicit instructions. See
  "next planned phase."
- **The "task exists → gets removed" path in `remove_task.ps1` is untested
  end-to-end** - registering even a throwaway test task requires elevation, which
  this environment doesn't have non-interactively. Covered only by a source-level
  regression check that the `-ConfirmRemove` guard precedes the real
  `Unregister-ScheduledTask` call; worth a manual elevated-session smoke test
  before relying on it in anger.
- **`single_instance_lock()` is Windows-only** (`msvcrt`) - deliberate, matches this
  feature's Windows/Task Scheduler scope, but the code would need a different
  implementation (e.g. `fcntl`) to ever run on Linux/macOS.
- **The launcher's raw per-run log (`logs\poll_<date>.log`) rotates only by
  calendar day** - no size cap or automatic pruning of old daily files. Fine at one
  run every 5 minutes (~288 short entries/day), but old files accumulate
  indefinitely unless cleaned up manually.
- **`production-check`'s SMTP validation is structural only** (host/port/address
  shape, no connection attempt) - a correctly-shaped but wrong password or an
  unreachable host would still show `[PASS]`; only `test-email` actually proves
  delivery works.
- **No fixture for a genuinely full/closed section's exact rendering beyond the
  `0/<capacity>` number** confirmed in `unc_class_search_tiled_subject_all.html`.
- **Cross-listed courses** ("Same As" field, confirmed present in the real HTML) are
  parsed but not modeled.
- **CSRF token is never refreshed mid-run.**
- **Wire-level request requirements aren't fully confirmed** — `Referer` header is
  sent proactively as a defensive guess, not empirically verified as required.
- **`poll-once-live`'s seeded `seats_capacity` is a fixed placeholder (`1`)**, not a
  real captured capacity.
- `.env` currently holds this project's real local configuration; not inspected or
  read as part of this status file (only exercised indirectly via the redacted
  `production-check` run recorded above).
