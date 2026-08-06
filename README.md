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
