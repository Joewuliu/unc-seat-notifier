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
    register_task.ps1             # previews, and (with -ConfirmRegister) registers,
                                    # the scheduled task
    remove_task.ps1                # previews, and (with -ConfirmRemove) removes it
    verify_task.ps1                  # read-only status report for the registered task
    show_task_scheduler_command.ps1    # superseded by register_task.ps1 - kept only
                                        # as a historical schtasks.exe-based reference
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

`scripts/register_task.ps1` registers the task with the
[`ScheduledTasks`](https://learn.microsoft.com/powershell/module/scheduledtasks/)
PowerShell module rather than `schtasks.exe` - several of the required settings
(execution time limit, restart count/interval, "start as soon as possible after a
missed start") aren't expressible through `schtasks.exe`'s `/Create` flags, but map
directly onto that module's cmdlets. **Default mode only ever previews** - it never
calls `Register-ScheduledTask` unless you pass `-ConfirmRegister` explicitly.

#### The exact safe sequence

```powershell
# a. Confirm everything poll needs is actually in place (read-only):
.\.venv\Scripts\python.exe -m unc_notifier production-check

# b. Run the real launcher once by hand, exactly as Task Scheduler will invoke it:
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_poll.ps1

# c. Preview the registration - prints every setting, registers nothing:
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1

# d. Only once you've reviewed (c) and are ready: register for real. This requires
#    an elevated ("Run as Administrator") PowerShell session - Register-ScheduledTask
#    fails with "Access is denied" otherwise:
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -ConfirmRegister

# e. Verify it's really there and picking up runs (read-only):
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify_task.ps1

# f. Inspect logs after a few runs:
#    logs\poll_<date>.log        - raw stdout/stderr per run, dated
#    logs\unc_notifier.log       - structured JSON-lines detail (fetches, state
#                                  transitions, notification attempts/outcomes)
#    Task Scheduler's own History tab - run start/end times and exit codes

# g. Disable or remove when you want to stop:
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\remove_task.ps1              # preview
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\remove_task.ps1 -ConfirmRemove  # for real
# - or, to pause without deleting the registration:
Disable-ScheduledTask -TaskName "UNC Seat Notifier"
```

#### What gets registered

| Setting | Value |
|---|---|
| Task name | `UNC Seat Notifier` |
| Program | the resolved path to `powershell.exe` |
| Arguments | `-NoProfile -ExecutionPolicy Bypass -File "<repo>\scripts\run_poll.ps1"` |
| Start in | the repository root (though the launcher resolves its own paths regardless) |
| Trigger | every 5 minutes, starting immediately, indefinitely |
| User account context | `SYSTEM` - runs whether logged on or not, with no stored password (SYSTEM has none) |
| Run level | Limited (standard user privileges, not "highest") |
| Overlap behavior | `MultipleInstances=IgnoreNew` - a second scheduled run is skipped while one is still in progress. Belt-and-suspenders: `poll` already refuses to run concurrently on its own via `single_instance_lock()` |
| Maximum runtime | 4 minutes (a poll normally finishes in seconds - generous safety ceiling) |
| Restart on failure | up to 3 times, 1 minute apart |
| Missed-start behavior | starts as soon as possible if a scheduled run was missed (e.g. the computer was off) |
| Runs on battery | yes, and does not stop if switching to battery mid-run |
| Wakes the computer | no |
| Logs | `logs\poll_<date>.log` (launcher) and `logs\unc_notifier.log` (structured) |

`scripts\register_task.ps1`'s preview output is generated directly from the exact
objects passed to `Register-ScheduledTask` (not a separate hand-written
description), so what you see previewed is exactly what gets registered.

#### Why the trigger has no explicit repetition end date

A first real registration attempt failed with:

```
Register-ScheduledTask: The task XML contains a value which is incorrectly
formatted or out of range. (8,42):Duration:P99999999DT23H59M59S
HRESULT 0x80041318
```

caused by the trigger setting `-RepetitionDuration ([TimeSpan]::MaxValue)`, meant
to express "repeat every 5 minutes forever." `[TimeSpan]::MaxValue` serializes to
the ISO-8601 duration `P99999999DT23H59M59S` (~273,785 years), which the *live*
Task Scheduler service rejects as out of range at registration time - even though
building that same value into a trigger object locally doesn't error until you
actually try to register it.

The fix: **omit `-RepetitionDuration` entirely.** Per Microsoft's own
documentation, leaving it unset while `-RepetitionInterval` is set means the
repetition never ends - and building the equivalent trigger directly against the
Task Scheduler COM API (`Schedule.Service` - no elevation needed, since it only
builds an in-memory task definition and never calls the service) confirms the
resulting XML has no `<Duration>` element under `<Repetition>` at all, which is the
schema-correct "repeat indefinitely" shape. This is what `register_task.ps1` does
by default now.

If a future Windows version or policy ever requires an explicit (non-omitted)
Duration for repetition to actually take effect, `register_task.ps1` has an opt-in
`-RepetitionYears N` fallback (e.g. `-RepetitionYears 10`) that sets a finite,
ordinary-sized Duration instead (`P3650D` for 10 years - nowhere near the value
that failed). Unlike the default, a finite Duration means the trigger **stops
firing new repetitions once that many years have elapsed since registration** -
Task Scheduler does not auto-renew it, so you'd need to re-run `register_task.ps1
-ConfirmRegister` again before then to keep polling going. This fallback is
documented but not the recommended path, and hasn't been verified against a live,
elevated registration (unavailable in this environment).

**Never scrapes UNC, sends email, or embeds any secret** - the registered command
is only ever `powershell.exe -NoProfile -ExecutionPolicy Bypass -File
"...\run_poll.ps1"`; `run_poll.ps1` itself reads `.env` at run time, so no
credential is ever written into the task definition, and none of `register_task.ps1`
/`remove_task.ps1`/`verify_task.ps1` reads or prints `SMTP_USERNAME`/`SMTP_PASSWORD`.
Registering under a real user account instead of `SYSTEM` (not what these scripts
do by default) is the one case where Windows would prompt you for that account's
password at registration time, to store it for unattended logon.

### Rollback / disable

To stop scheduled polling without deleting anything: `Disable-ScheduledTask
-TaskName "UNC Seat Notifier"` (keeps the registration, stops it from running) or
`scripts\remove_task.ps1 -ConfirmRemove` (removes the registration entirely -
`run_poll.ps1`, `.env`, `watches.yaml`, and `data\unc_notifier.db` are all untouched
either way, so registering again later picks up exactly where it left off).
