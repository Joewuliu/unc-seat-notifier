"""SmtpNotifier tests mock smtplib entirely — no real network/SMTP connection is made."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from unc_notifier.models import NotificationEvent
from unc_notifier.notifier.smtp import SmtpConfig, SmtpNotifier

from .helpers import SECTION, dt


def make_event(**overrides) -> NotificationEvent:
    defaults = dict(
        section_key=SECTION,
        seats_available=3,
        seats_capacity=20,
        waitlist_available=10,
        waitlist_capacity=10,
        status="open",
        reason="transition_available",
        level="info",
        occurred_at=dt(0),
    )
    defaults.update(overrides)
    return NotificationEvent(**defaults)


def make_config(**overrides) -> SmtpConfig:
    defaults = dict(
        host="smtp.example.com",
        port=587,
        username="user",
        password="secret",
        use_tls=True,
        from_addr="bot@example.com",
        to_addr="me@example.com",
    )
    defaults.update(overrides)
    return SmtpConfig(**defaults)


@patch("unc_notifier.notifier.smtp.smtplib.SMTP")
def test_notify_sends_message_with_tls_and_login(mock_smtp_cls: MagicMock):
    client = mock_smtp_cls.return_value.__enter__.return_value
    notifier = SmtpNotifier(make_config())

    notifier.notify(make_event())

    client.starttls.assert_called_once()
    client.login.assert_called_once_with("user", "secret")
    client.send_message.assert_called_once()
    sent = client.send_message.call_args[0][0]
    assert sent["From"] == "bot@example.com"
    assert sent["To"] == "me@example.com"
    assert "COMP 550-001" in sent["Subject"]


@patch("unc_notifier.notifier.smtp.smtplib.SMTP")
def test_notify_skips_login_when_no_credentials(mock_smtp_cls: MagicMock):
    client = mock_smtp_cls.return_value.__enter__.return_value
    notifier = SmtpNotifier(make_config(username=None, password=None, use_tls=False))

    notifier.notify(make_event())

    client.starttls.assert_not_called()
    client.login.assert_not_called()
    client.send_message.assert_called_once()


@patch("unc_notifier.notifier.smtp.smtplib.SMTP")
def test_notify_marks_waitlist_warning_in_subject(mock_smtp_cls: MagicMock):
    client = mock_smtp_cls.return_value.__enter__.return_value
    notifier = SmtpNotifier(make_config())

    notifier.notify(make_event(level="warning", reason="waitlist_warn"))

    sent = client.send_message.call_args[0][0]
    assert sent["Subject"].startswith("[waitlist warning]")


@patch("unc_notifier.notifier.smtp.smtplib.SMTP")
def test_notify_reports_unknown_waitlist_honestly(mock_smtp_cls: MagicMock):
    """Real UNC data never exposes waitlist counts - the email must say "unknown,"
    never fabricate "0/0 occupied" which would falsely claim a confirmed empty waitlist."""
    client = mock_smtp_cls.return_value.__enter__.return_value
    notifier = SmtpNotifier(make_config())

    notifier.notify(make_event(waitlist_available=None, waitlist_capacity=None))

    sent = client.send_message.call_args[0][0]
    body = sent.get_content()
    assert "Waitlist: unknown" in body
