"""State-machine tests. Pure inputs/outputs — no DB, no network, no SMTP.

These map directly to the scenarios required by SPEC.md's "Tests" section, plus the
pending-notification / retry bookkeeping the state machine now owns.
"""

from __future__ import annotations

from datetime import timedelta

from unc_notifier import diff
from unc_notifier.models import WaitlistPolicy

from .helpers import dt, make_snapshot, make_state

COOLDOWN = timedelta(minutes=30)
RETRY_DELAY = timedelta(minutes=5)


def evaluate(prev, snapshot, *, waitlist_policy, cooldown=COOLDOWN, retry_delay=RETRY_DELAY):
    return diff.evaluate(
        prev, snapshot, waitlist_policy=waitlist_policy, cooldown=cooldown, retry_delay=retry_delay
    )


def test_first_observation_does_not_notify():
    snapshot = make_snapshot(seats_available=0, observed_at=dt(0))

    decision = evaluate(None, snapshot, waitlist_policy=WaitlistPolicy.ALERT)

    assert decision.should_attempt_notify is False
    assert decision.reason == diff.REASON_FIRST_OBSERVATION
    # State is still recorded even though we don't notify.
    assert decision.new_state.seats_available == 0
    assert decision.new_state.last_notified_at is None
    assert decision.new_state.pending_notification_since is None
    assert decision.new_state.last_notification_attempt_at is None


def test_zero_to_available_notifies_exactly_once():
    prev = make_state(seats_available=0, last_notified_at=None)
    snapshot = make_snapshot(seats_available=3, waitlist_capacity=0, waitlist_available=0)

    decision = evaluate(prev, snapshot, waitlist_policy=WaitlistPolicy.ALERT)

    assert decision.should_attempt_notify is True
    assert decision.reason == diff.REASON_TRANSITION_AVAILABLE
    # last_notified_at is NOT advanced yet - only a successful send does that. A
    # pending-delivery marker is set instead, for the poller to resolve.
    assert decision.new_state.last_notified_at is None
    assert decision.new_state.pending_notification_since == snapshot.observed_at
    # last_notification_attempt_at is untouched by the pure decision - deciding an
    # attempt is due is not the same as making one. prev never had one, so it's still
    # None; only the poller, right before it actually calls notify(), may set it.
    assert decision.new_state.last_notification_attempt_at is None


def test_available_to_available_does_not_renotify():
    prev = make_state(seats_available=3, last_notified_at=dt(0))
    snapshot = make_snapshot(seats_available=3, observed_at=dt(1))

    decision = evaluate(prev, snapshot, waitlist_policy=WaitlistPolicy.ALERT)

    assert decision.should_attempt_notify is False
    assert decision.reason == diff.REASON_STILL_AVAILABLE
    # last_notified_at is carried forward, not cleared or bumped.
    assert decision.new_state.last_notified_at == dt(0)
    assert decision.new_state.pending_notification_since is None


def test_flap_within_cooldown_notifies_exactly_once():
    """0 -> 3 -> 0 -> 3, all within the cooldown window, should notify only on the first
    flip. Each attempt in this test is treated as an immediate successful send (that's
    what advances last_notified_at) - retry bookkeeping is covered separately below."""
    policy = WaitlistPolicy.ALERT

    state = make_state(seats_available=0, last_notified_at=None, last_seen_at=dt(0))

    # 0 -> 3 at minute 1: should attempt.
    d1 = evaluate(
        state, make_snapshot(seats_available=3, observed_at=dt(1)), waitlist_policy=policy
    )
    assert d1.should_attempt_notify is True
    # Simulate what the poller does on a successful send.
    state = make_state(
        seats_available=3,
        last_notified_at=dt(1),
        last_seen_at=dt(1),
        pending_notification_since=None,
        last_notification_attempt_at=None,
    )

    # 3 -> 0 at minute 2: no notification (not a transition into availability).
    d2 = evaluate(
        state, make_snapshot(seats_available=0, observed_at=dt(2)), waitlist_policy=policy
    )
    assert d2.should_attempt_notify is False
    assert d2.reason == diff.REASON_BECAME_UNAVAILABLE
    state = d2.new_state

    # 0 -> 3 again at minute 5, still inside the 30-minute cooldown: suppressed.
    d3 = evaluate(
        state, make_snapshot(seats_available=3, observed_at=dt(5)), waitlist_policy=policy
    )
    assert d3.should_attempt_notify is False
    assert d3.reason == diff.REASON_COOLDOWN_SUPPRESSED
    # last_notified_at is still the minute-1 notification.
    assert d3.new_state.last_notified_at == dt(1)
    assert d3.new_state.pending_notification_since is None


def test_transition_outside_cooldown_notifies_again():
    prev = make_state(seats_available=0, last_notified_at=dt(0))
    snapshot = make_snapshot(seats_available=5, observed_at=dt(31))

    decision = evaluate(prev, snapshot, waitlist_policy=WaitlistPolicy.ALERT)

    assert decision.should_attempt_notify is True
    assert decision.new_state.pending_notification_since == dt(31)


def test_waitlist_active_silent_policy_suppresses_notification():
    prev = make_state(seats_available=0, last_notified_at=None)
    snapshot = make_snapshot(
        seats_available=1, waitlist_capacity=10, waitlist_available=7  # 3 people waiting
    )

    decision = evaluate(prev, snapshot, waitlist_policy=WaitlistPolicy.SILENT)

    assert decision.should_attempt_notify is False
    assert decision.reason == diff.REASON_WAITLIST_SILENT
    assert decision.new_state.pending_notification_since is None


def test_waitlist_active_warn_policy_notifies_with_warning_level():
    prev = make_state(seats_available=0, last_notified_at=None)
    snapshot = make_snapshot(seats_available=1, waitlist_capacity=10, waitlist_available=7)

    decision = evaluate(prev, snapshot, waitlist_policy=WaitlistPolicy.WARN)

    assert decision.should_attempt_notify is True
    assert decision.reason == diff.REASON_WAITLIST_WARN
    assert decision.notify_level == diff.LEVEL_WARNING


def test_waitlist_active_alert_policy_notifies_normally():
    prev = make_state(seats_available=0, last_notified_at=None)
    snapshot = make_snapshot(seats_available=1, waitlist_capacity=10, waitlist_available=7)

    decision = evaluate(prev, snapshot, waitlist_policy=WaitlistPolicy.ALERT)

    assert decision.should_attempt_notify is True
    assert decision.reason == diff.REASON_TRANSITION_AVAILABLE
    assert decision.notify_level == diff.LEVEL_INFO


def test_waitlist_with_no_one_on_it_is_not_treated_as_active():
    """waitlist_capacity > 0 but waitlist_available == waitlist_capacity means nobody's waiting."""
    prev = make_state(seats_available=0, last_notified_at=None)
    snapshot = make_snapshot(seats_available=1, waitlist_capacity=10, waitlist_available=10)

    decision = evaluate(prev, snapshot, waitlist_policy=WaitlistPolicy.SILENT)

    # Policy is SILENT-for-active-waitlists, but this waitlist isn't active, so it still fires.
    assert decision.should_attempt_notify is True
    assert decision.reason == diff.REASON_TRANSITION_AVAILABLE


def test_successful_observation_resets_consecutive_failures():
    prev = make_state(seats_available=0, consecutive_failures=4)
    snapshot = make_snapshot(seats_available=0, observed_at=dt(1))

    decision = evaluate(prev, snapshot, waitlist_policy=WaitlistPolicy.ALERT)

    assert decision.new_state.consecutive_failures == 0


# --- Pending-notification / retry bookkeeping ---------------------------------------


def test_pending_notification_not_yet_due_for_retry():
    """A previous attempt failed 2 minutes ago; retry_delay is 5 minutes - not due yet."""
    prev = make_state(
        seats_available=3,
        last_notified_at=None,
        pending_notification_since=dt(0),
        last_notification_attempt_at=dt(0),
    )
    snapshot = make_snapshot(seats_available=3, observed_at=dt(2))

    decision = evaluate(prev, snapshot, waitlist_policy=WaitlistPolicy.ALERT)

    assert decision.should_attempt_notify is False
    assert decision.reason == diff.REASON_NOTIFICATION_RETRY_PENDING
    # Bookkeeping carried forward unchanged - still waiting on the original attempt.
    assert decision.new_state.pending_notification_since == dt(0)
    assert decision.new_state.last_notification_attempt_at == dt(0)


def test_pending_notification_retries_once_delay_elapses():
    prev = make_state(
        seats_available=3,
        last_notified_at=None,
        pending_notification_since=dt(0),
        last_notification_attempt_at=dt(0),
    )
    snapshot = make_snapshot(seats_available=3, observed_at=dt(6))  # 6 min later > 5 min delay

    decision = evaluate(prev, snapshot, waitlist_policy=WaitlistPolicy.ALERT)

    assert decision.should_attempt_notify is True
    assert decision.reason == diff.REASON_NOTIFICATION_RETRY
    # The original pending_notification_since is preserved across retries.
    assert decision.new_state.pending_notification_since == dt(0)
    # Deciding a retry is due does not itself count as making one - the pure decision
    # leaves the real attempt-time bookkeeping exactly as prev had it (dt(0), from the
    # earlier failed attempt), NOT snapshot.observed_at (dt(6)). Only the poller, right
    # before it actually calls notify(), may advance this field.
    assert decision.new_state.last_notification_attempt_at == dt(0)
    assert decision.new_state.last_notified_at is None


def test_pending_notification_is_not_cooldown_gated():
    """Retries of an outstanding transition are governed only by retry_delay, never by
    the (much longer) cooldown - cooldown debounces *new* transitions after a success."""
    prev = make_state(
        seats_available=3,
        last_notified_at=None,
        pending_notification_since=dt(0),
        last_notification_attempt_at=dt(0),
    )
    snapshot = make_snapshot(seats_available=3, observed_at=dt(6))

    decision = evaluate(
        prev, snapshot, waitlist_policy=WaitlistPolicy.ALERT, cooldown=timedelta(hours=6)
    )

    assert decision.should_attempt_notify is True
    assert decision.reason == diff.REASON_NOTIFICATION_RETRY


def test_section_closing_before_retry_clears_pending_notification():
    prev = make_state(
        seats_available=3,
        last_notified_at=None,
        pending_notification_since=dt(0),
        last_notification_attempt_at=dt(0),
    )
    snapshot = make_snapshot(seats_available=0, observed_at=dt(2))

    decision = evaluate(prev, snapshot, waitlist_policy=WaitlistPolicy.ALERT)

    assert decision.should_attempt_notify is False
    assert decision.reason == diff.REASON_PENDING_CLEARED_UNAVAILABLE
    assert decision.new_state.pending_notification_since is None
    assert decision.new_state.last_notification_attempt_at is None
    # Never having succeeded, last_notified_at stays None.
    assert decision.new_state.last_notified_at is None


def test_evaluate_never_advances_last_notification_attempt_at():
    """diff.evaluate() deciding an attempt is due is not the same as an attempt having
    happened - last_notification_attempt_at must come out exactly as it went in,
    regardless of should_attempt_notify, for both a fresh transition and a due retry."""
    # Fresh transition: prev never had an attempt recorded - stays None.
    fresh_prev = make_state(seats_available=0, last_notified_at=None)
    assert fresh_prev.last_notification_attempt_at is None
    fresh_decision = evaluate(
        fresh_prev, make_snapshot(seats_available=3, observed_at=dt(5)),
        waitlist_policy=WaitlistPolicy.ALERT,
    )
    assert fresh_decision.should_attempt_notify is True
    assert fresh_decision.new_state.last_notification_attempt_at is None

    # Due retry: prev already carries an attempt time from an earlier failure -
    # evaluate() must pass it through completely untouched, not bump it to "now".
    retry_prev = make_state(
        seats_available=3,
        last_notified_at=None,
        pending_notification_since=dt(0),
        last_notification_attempt_at=dt(0),
    )
    retry_decision = evaluate(
        retry_prev, make_snapshot(seats_available=3, observed_at=dt(10)),
        waitlist_policy=WaitlistPolicy.ALERT,
    )
    assert retry_decision.should_attempt_notify is True
    assert retry_decision.reason == diff.REASON_NOTIFICATION_RETRY
    assert retry_decision.new_state.last_notification_attempt_at == dt(0)
