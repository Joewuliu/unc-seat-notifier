# unc-seat-notifier

Single-user CLI/cron seat-availability notifier for UNC Chapel Hill course sections.
See [SPEC.md](SPEC.md) for the full project specification.

## Setup

Copy `.env.example` to `.env` and `watches.yaml.example` to `watches.yaml`, then fill
in your SMTP and course-watch details.

## Commands

### `poll`

Runs one poll cycle (fetch every watched course, diff against last-known state,
send email on a real transition into availability) and exits. Intended to be invoked
repeatedly by an external scheduler (cron / Task Scheduler) - see SPEC.md.

```
python -m unc_notifier poll
```

### `scrape-once`

A read-only smoke test for the real UNC scraper. Fetches and prints one course's
sections directly from UNC's live Tiled Search, without touching email, the database,
or any persisted state - safe to run at any time to verify the scraper still works
against the real site.

```
python -m unc_notifier scrape-once --term "2026 Fall" --subject COMP --catalog-number 210
```

Exits `0` on success (including a legitimate zero-results course) and non-zero with a
clear message on any fetch or parse failure. Still reads `.env` for `CONTACT_EMAIL`
(used in the scraper's User-Agent), but never constructs a `Repository`, `SmtpNotifier`,
or opens the production database.

### `poll-once-dry-run`

Exercises the real scraper, the real state-machine decision logic (`diff.evaluate`),
and a real `Repository` together for exactly one section - the full pipeline the real
`poll` command uses, minus SMTP. Notifications print to the console
(`ConsoleNotifier`) instead of being emailed. `--db-path` is required; there is no
default, so this can never accidentally read or write the production database.

```
python -m unc_notifier poll-once-dry-run \
  --term "2026 Fall" --subject COMP --catalog-number 210 --section 001 \
  --db-path .tmp/unc-seat-notifier-test.db
```

Prints, in order: the fetched seat state, the previously stored state (if any), the
detected transition, the notification decision (reason/level), whether a notification
would have been sent, and the final stored state. Safe to run repeatedly against the
same `--db-path` - each run just diffs against whatever that file already has, exactly
like a real poll cycle would (first run records a baseline with no notification; a
later run only notifies on a genuine transition into availability, subject to the same
cooldown/retry rules the real poller uses). Point `--db-path` at a scratch file (or
delete it) to start over.

### `test-email`

Sends exactly one clearly-labeled test email via the real `SmtpNotifier`, to verify
SMTP configuration. Only requires `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, `SMTP_TO` (and
`SMTP_USERNAME`/`SMTP_PASSWORD`/`SMTP_USE_TLS` if your relay needs them) - it does not
read `CONTACT_EMAIL`, `watches.yaml`, or any database setting, and never constructs
`UncClassSearchScraper`, `Repository`, `Poller`, or a SQLite connection.

```
python -m unc_notifier test-email
```

The email's subject is always `[UNC Seat Notifier] Test Email` and its body states
plainly that it's a configuration test, not a real seat alert. Prints
`Test email sent successfully.` only after the SMTP server has actually accepted the
message (i.e. `smtplib`'s send call returned without raising); on failure it prints a
clear `SMTP test email failed: ...` message to stderr and exits non-zero.
`SMTP_USERNAME`/`SMTP_PASSWORD` are never printed, including inside error messages.

### `poll-once-live`

Runs the *real* end-to-end poll for exactly one section: real `UncClassSearchScraper`,
real `Repository`, real `Poller.apply_snapshot()` (the same notification-resolution
code path `poll` uses - see PROJECT_STATUS.md), and the real `SmtpNotifier`. This is
the only diagnostic command that can send a real seat-alert email, so it requires two
things `poll-once-dry-run` doesn't:

- `--db-path` (required, no default - never touches the production database).
- `--confirm-send-email` - without it, the command refuses to run at all (it never
  even fetches or constructs `SmtpNotifier`). Use `poll-once-dry-run` instead if you
  don't want to risk sending real email.

```
python -m unc_notifier poll-once-live \
  --term "2026 Fall" --subject COMP --catalog-number 210 --section 001 \
  --db-path .tmp/live-test.db --confirm-send-email
```

Prints, in order: the database path, the fetched seat state, the previous stored
state, the detected transition, the notification decision, whether email was
attempted, whether it succeeded, and the final stored state. If SMTP delivery fails,
the fetched seat state is still persisted and the notification stays pending/retryable
- exactly like a real poll's failure handling - and the command still exits `0` (the
poll itself succeeded; the email is retryable on a later run, the same as production).
`SMTP_USERNAME`/`SMTP_PASSWORD` are never printed.

#### Safe test sequence (no real course transition required)

Since UNC courses rarely close-then-reopen on demand, use `--seed-closed-state` to set
up a synthetic starting point, then run a real live poll to see a genuine
closed-to-open email:

```
# 1. Seed a synthetic "closed" prior state - no network call, no email:
python -m unc_notifier poll-once-live \
  --term "2026 Fall" --subject COMP --catalog-number 210 --section 001 \
  --db-path .tmp/live-test.db --seed-closed-state --confirm-seed

# 2. Run the real live poll against the same --db-path. If the section is actually
#    open right now, this sends one real email (closed -> open transition):
python -m unc_notifier poll-once-live \
  --term "2026 Fall" --subject COMP --catalog-number 210 --section 001 \
  --db-path .tmp/live-test.db --confirm-send-email

# 3. Run it again - same state, no duplicate email:
python -m unc_notifier poll-once-live \
  --term "2026 Fall" --subject COMP --catalog-number 210 --section 001 \
  --db-path .tmp/live-test.db --confirm-send-email
```

`--seed-closed-state` writes only to `--db-path`, makes no network call, and never
touches `course_fetch_health` or the observations table (it's explicitly synthetic
test data, not a real observation). Delete `.tmp/live-test.db` to start over.

### `production-check`

Read-only validation that everything the real `poll` command needs is present and
structurally valid - safe to run any time, as often as you like:

```
python -m unc_notifier production-check
```

Checks (each printed as `[PASS]`/`[FAIL]` with a detail): configuration parses,
watches file exists and parses, the database/log/lock directories are writable
(they don't need to exist yet - `poll` creates them on first run - but their nearest
existing parent must be writable), SMTP settings are structurally valid (host/port/
addresses present and sane - no connection is ever attempted), and the scraper's
`CONTACT_EMAIL` is present. Also prints the Python interpreter path/version and
whether it's running inside a virtual environment. Exits `0` only if every check
passes. Never scrapes UNC, sends email, or creates/modifies the production database
- the only filesystem write anywhere in this command is a temp probe file used to
test directory writability, created and deleted immediately. Never prints
`SMTP_USERNAME`/`SMTP_PASSWORD`.

## Production deployment (Windows Task Scheduler)

This runs `poll` unattended, every 5 minutes, via Windows Task Scheduler. Everything
below can be prepared and tested without registering the scheduled task itself - see
"Task Scheduler setup" for the one remaining manual step.

### Directory layout

```
unc-seat-notifier/
  .env                    # your real config - never committed (see .env.example)
  watches.yaml             # your real watch list - never committed (see watches.yaml.example)
  .venv/                    # virtual environment (python -m venv .venv)
  data/
    unc_notifier.db          # SQLite state (DB_PATH)
    unc_notifier.lock         # single-instance lock file (LOCK_PATH)
  logs/
    poll_20260806.log          # launcher's raw stdout/stderr, one file per day
    unc_notifier.log            # structured JSON-lines application log (LOG_PATH)
  scripts/
    run_poll.ps1                 # the Task Scheduler launcher
    show_task_scheduler_command.ps1  # prints (never runs) the schtasks command
```

`DB_PATH`, `LOG_PATH`, `WATCHES_PATH`, and `LOCK_PATH` in `.env` are all resolved
relative to `.env`'s own directory when relative (not the process's working
directory) - so it doesn't matter what "Start in" folder Task Scheduler uses. Use
absolute paths in `.env` if you'd rather be fully explicit.

### Required `.env` values

Copy `.env.example` to `.env` in the repository root and fill in at minimum
`CONTACT_EMAIL`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, `SMTP_TO` (plus
`SMTP_USERNAME`/`SMTP_PASSWORD` if your relay needs auth). Everything else
(`DB_PATH`, `LOG_PATH`, `WATCHES_PATH`, `LOCK_PATH`, `POLL_INTERVAL_MINUTES`,
`COOLDOWN_MINUTES`, etc.) has a sensible default - see the comments in
`.env.example`.

### Watches configuration

Copy `watches.yaml.example` to `watches.yaml` and edit it to your real courses.
`term`/`subject`/`catalog_number`/`section` are always strings (`catalog_number`
values like `283H` and leading-zero section numbers are common at UNC), e.g.:

```yaml
watches:
  - term: "2026 Fall"
    subject: "COMP"
    catalog_number: "210"
    section: "001"
```

Nothing in this project ever writes to `watches.yaml` automatically - it's yours to
edit by hand.

### Manual launcher test (before scheduling anything)

```powershell
# 1. Sanity-check path resolution without touching Python, the venv, or logs\:
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_poll.ps1 -DryRun

# 2. Confirm everything poll needs is actually in place:
.\.venv\Scripts\python.exe -m unc_notifier production-check

# 3. Run the real launcher once, exactly as Task Scheduler will invoke it, from an
#    unrelated working directory (proves it doesn't depend on "Start in"):
cd C:\Windows\System32
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\path\to\unc-seat-notifier\scripts\run_poll.ps1"
```

Step 3 runs a real poll (real scraper, real SMTP) - only run it once you're ready,
the same way you'd verify `poll` itself. Check `logs\poll_<date>.log` for the raw
run markers and `logs\unc_notifier.log` for the structured per-course/per-section
detail afterward.

### Task Scheduler setup

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\show_task_scheduler_command.ps1
```

This prints the exact `schtasks /create ...` command to register the task (program:
`powershell.exe`, arguments: `-NoProfile -ExecutionPolicy Bypass -File
"...\scripts\run_poll.ps1"`, no "Start in" directory needed since the launcher
resolves its own paths) - **it does not run it**. Review the printed command, then
run it yourself (as Administrator) when ready. It registers the task to run every 5
minutes, whether the user is logged on or not (`/RU SYSTEM`, which avoids having to
store a password for "run whether logged on or not"), limited to standard user
privileges (`/RL LIMITED`).

The script also prints a few settings that only exist in Task Scheduler's own
Properties dialog (not expressible via `schtasks /create` flags) to set by hand
afterward: "stop the task if it runs longer than" (a generous safety ceiling - a
poll should finish in seconds), "restart on failure", and "do not start a new
instance if one is already running" (belt-and-suspenders alongside `poll`'s own
`single_instance_lock()`, which already refuses a second overlapping run on its
own).

To inspect what happened after the fact: Task Scheduler's own history tab shows
run start/end times and exit codes; `logs\poll_<date>.log` has the raw
console/error output for that day; `logs\unc_notifier.log` (JSON-lines) has the
structured detail - per-course fetch results, state transitions, notification
attempts and outcomes, for every run.

### Rollback / disable

To stop scheduled polling without deleting anything: open Task Scheduler, find
"UNC Seat Notifier", and either **Disable** it (keeps the task definition, stops it
from running) or **Delete** it (removes the task entirely - `run_poll.ps1`,
`.env`, `watches.yaml`, and `data\unc_notifier.db` are all untouched either way, so
re-registering later picks up exactly where it left off).
