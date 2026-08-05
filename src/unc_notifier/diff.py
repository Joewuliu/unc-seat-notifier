"""The state machine: decides whether a new, already-validated snapshot should attempt
a notification, and manages the pending-delivery bookkeeping across retries.

Pure functions only. No SQLite, no SMTP, no requests, no environment access, no wall-clock
reads (time comes from ``SeatSnapshot.observed_at``, supplied by the caller). Fetch failures
and malformed responses never reach this module — the poller/provider layer is responsible
for turning a raw fetch into a valid SeatSnapshot (or handling the failure itself) before
calling here. Whether an attempted notification actually *succeeds* is likewise not this
module's concern - it decides only whether an attempt should be made this poll; the poller
resolves the outcome and persists the final state accordingly.
"""

from __future__ import annotations

from datetime import timedelta

from unc_notifier.models import Decision, SeatSnapshot, SectionState, WaitlistPolicy

REASON_FIRST_OBSERVATION = "first_observation"
REASON_STILL_AVAILABLE = "still_available"
REASON_STILL_UNAVAILABLE = "still_unavailable"
REASON_BECAME_UNAVAILABLE = "became_unavailable"
REASON_TRANSITION_AVAILABLE = "transition_available"
REASON_WAITLIST_SILENT = "waitlist_silent"
REASON_WAITLIST_WARN = "waitlist_warn"
REASON_COOLDOWN_SUPPRESSED = "cooldown_suppressed"
REASON_NOTIFICATION_RETRY = "notification_retry"
REASON_NOTIFICATION_RETRY_PENDING = "notification_retry_pending"
REASON_PENDING_CLEARED_UNAVAILABLE = "pending_notification_cleared"

LEVEL_INFO = "info"
LEVEL_WARNING = "warning"


def _state_from_snapshot(
    snapshot: SeatSnapshot,
    *,
    last_notified_at,
    pending_notification_since,
    last_notification_attempt_at,
) -> SectionState:
    return SectionState(
        section_key=snapshot.section_key,
        seats_available=snapshot.seats_available,
        seats_capacity=snapshot.seats_capacity,
        waitlist_available=snapshot.waitlist_available,
        waitlist_capacity=snapshot.waitlist_capacity,
        status=snapshot.status,
        last_seen_at=snapshot.observed_at,
        last_notified_at=last_notified_at,
        consecutive_failures=0,
        last_attempt_at=snapshot.observed_at,
        pending_notification_since=pending_notification_since,
        last_notification_attempt_at=last_notification_attempt_at,
    )


def evaluate(
    prev: SectionState | None,
    snapshot: SeatSnapshot,
    *,
    waitlist_policy: WaitlistPolicy,
    cooldown: timedelta,
    retry_delay: timedelta,
) -> Decision:
    """Decide what to do with one section's newly fetched, validated snapshot.

    ``prev`` is the last-known state for this section, or ``None`` if this is the first
    time it has ever been observed. When ``should_attempt_notify`` is True, this only
    means an attempt is *due* - it deliberately leaves ``last_notification_attempt_at``
    exactly as ``prev`` had it, never stamping it with ``snapshot.observed_at``. Only the
    poller, immediately before it actually calls ``notifier.notify()``, may advance that
    field - see ``Poller._attempt_notify``. The caller must then persist the final state
    itself: on success, advance ``last_notified_at``, clear ``pending_notification_since``,
    and set ``last_notification_attempt_at`` to the real attempt time; on failure, persist
    ``decision.new_state`` with just ``last_notification_attempt_at`` overwritten to that
    same real attempt time - everything else it already carries is correct for a retry.
    """
    if prev is None:
        new_state = _state_from_snapshot(
            snapshot,
            last_notified_at=None,
            pending_notification_since=None,
            last_notification_attempt_at=None,
        )
        return Decision(
            new_state=new_state, should_attempt_notify=False, reason=REASON_FIRST_OBSERVATION
        )

    is_available = snapshot.is_available
    was_available = prev.is_available
    was_pending = prev.pending_notification_since is not None

    if not is_available:
        # Never attempt while unavailable, and a pending retry no longer applies -
        # the section closed again before we ever got a notification out.
        if was_pending:
            reason = REASON_PENDING_CLEARED_UNAVAILABLE
        elif was_available:
            reason = REASON_BECAME_UNAVAILABLE
        else:
            reason = REASON_STILL_UNAVAILABLE
        new_state = _state_from_snapshot(
            snapshot,
            last_notified_at=prev.last_notified_at,
            pending_notification_since=None,
            last_notification_attempt_at=None,
        )
        return Decision(new_state=new_state, should_attempt_notify=False, reason=reason)

    # From here, is_available is True.

    if was_available and not was_pending:
        # Steady state: already available, nothing outstanding to (re)send.
        new_state = _state_from_snapshot(
            snapshot,
            last_notified_at=prev.last_notified_at,
            pending_notification_since=None,
            last_notification_attempt_at=None,
        )
        return Decision(
            new_state=new_state, should_attempt_notify=False, reason=REASON_STILL_AVAILABLE
        )

    is_retry = was_available and was_pending

    if is_retry:
        attempt_due = (
            prev.last_notification_attempt_at is None
            or snapshot.observed_at - prev.last_notification_attempt_at >= retry_delay
        )
        if not attempt_due:
            new_state = _state_from_snapshot(
                snapshot,
                last_notified_at=prev.last_notified_at,
                pending_notification_since=prev.pending_notification_since,
                last_notification_attempt_at=prev.last_notification_attempt_at,
            )
            return Decision(
                new_state=new_state,
                should_attempt_notify=False,
                reason=REASON_NOTIFICATION_RETRY_PENDING,
            )
    else:
        # A fresh 0 -> available transition. Debounce against the last *successful*
        # notification - a pending retry (handled above) is never cooldown-gated.
        if prev.last_notified_at is not None:
            if snapshot.observed_at - prev.last_notified_at < cooldown:
                new_state = _state_from_snapshot(
                    snapshot,
                    last_notified_at=prev.last_notified_at,
                    pending_notification_since=None,
                    last_notification_attempt_at=None,
                )
                return Decision(
                    new_state=new_state,
                    should_attempt_notify=False,
                    reason=REASON_COOLDOWN_SUPPRESSED,
                )

    if snapshot.waitlist_active and waitlist_policy is WaitlistPolicy.SILENT:
        new_state = _state_from_snapshot(
            snapshot,
            last_notified_at=prev.last_notified_at,
            pending_notification_since=None,
            last_notification_attempt_at=None,
        )
        return Decision(
            new_state=new_state, should_attempt_notify=False, reason=REASON_WAITLIST_SILENT
        )

    if snapshot.waitlist_active and waitlist_policy is WaitlistPolicy.WARN:
        level = LEVEL_WARNING
    else:
        level = LEVEL_INFO

    if is_retry:
        reason = REASON_NOTIFICATION_RETRY
        pending_since = prev.pending_notification_since
    else:
        reason = REASON_WAITLIST_WARN if level == LEVEL_WARNING else REASON_TRANSITION_AVAILABLE
        pending_since = snapshot.observed_at

    new_state = _state_from_snapshot(
        snapshot,
        last_notified_at=prev.last_notified_at,
        pending_notification_since=pending_since,
        # Not snapshot.observed_at: deciding an attempt is due is not the same as
        # making one. Carried forward unchanged - the poller stamps the real attempt
        # time immediately before it actually calls notifier.notify().
        last_notification_attempt_at=prev.last_notification_attempt_at,
    )
    return Decision(
        new_state=new_state, should_attempt_notify=True, reason=reason, notify_level=level
    )
