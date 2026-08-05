I'm building a course seat-availability notifier for UNC Chapel Hill. Start with a
single-user CLI/cron version — no web app, no signup, no multi-tenancy yet.

## Stack
Python 3.11+, requests, SQLite (stdlib sqlite3 or SQLAlchemy — your call, justify it),
pytest. Keep dependencies minimal. Config via a .env file.

## CRITICAL CONSTRAINT — read this before writing any scraper code
The data source is UNC's public class search at https://reports.unc.edu/class-search/.
It is a JS app backed by a JSON endpoint I have NOT given you yet.

Do NOT guess, invent, or infer the endpoint URL, its query parameters, or its response
field names. Do not write a parser against an imagined schema.

Instead: build everything else first behind a clean interface, and define the scraper as
an abstract class with a fake/fixture-backed implementation so the rest of the system is
fully testable. Then STOP and ask me to paste a real request + JSON response captured
from my browser's DevTools Network tab. Only after I give you that should you write the
real parser.

## Phase 1 scope
1. A `watches.yaml` (or SQLite table) where I list courses to watch:
   term, subject, catalog number, optional section number.
2. A poller that runs on a schedule, fetches current seat data per course, and diffs it
   against last-known state.
3. Email notification via SMTP when a watched course transitions to available.
4. Structured logging to a file — every poll, every state change, every error.

## Data model
Store per (term, subject, catalog_number, section):
seats_available, seats_capacity, waitlist_available, waitlist_capacity, status,
last_seen_at, last_notified_at, consecutive_failures.
Also keep an append-only observations table so I can analyze how seat counts actually
move over time. I want the history, not just current state.

## State machine — this is where the bugs will be, treat it carefully
- Notify only on a transition INTO availability, not on every poll where seats > 0.
- A failed or malformed fetch must NEVER be treated as "0 seats" or as a state change.
  Increment consecutive_failures, log loudly, leave last-known state untouched.
- Debounce: do not re-notify for the same course within a configurable cooldown
  (default 30 min) even if it flaps 0 -> n -> 0 -> n.
- On first-ever observation of a course, record state but do NOT notify.
- Waitlist awareness: if a section has an active waitlist with people on it, an open seat
  is usually absorbed automatically. Model this explicitly and let me configure whether
  such courses should alert, alert with a warning, or stay silent.

## Tests (pytest, using fixture JSON — required, not optional)
- first observation -> no notification
- 0 -> 3 seats -> notification fired exactly once
- 3 -> 3 seats -> no notification
- fetch raises / returns garbage -> no notification, no state corruption
- 0 -> 3 -> 0 -> 3 within cooldown -> exactly one notification
- waitlist active with open seat -> respects configured policy

## Scraping etiquette — build this in from the start
- Configurable poll interval, default 5 minutes, with jitter.
- Deduplicate: poll once per distinct course, not once per watch entry.
- Descriptive User-Agent including a contact email (from .env).
- Exponential backoff on errors; hard circuit-breaker after N consecutive failures.
- Never request or store any student login credentials. This only ever touches the
  public, unauthenticated search.

## Working style
Ask me clarifying questions before making significant architectural decisions.
Explain tradeoffs briefly rather than silently picking. Write the tests alongside
the code, not after.