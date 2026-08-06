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
