import smtplib

import pytest

from postcard.core.net.smtp_session import SmtpError, SmtpSession


def test_send_raw_raises_when_never_connected() -> None:
    # Arrange: a session that was built but never connect()ed.
    session = SmtpSession("smtp.example.com", 465)

    # Act / Assert: it must fail loudly. Returning normally here would make
    # mail_sync.send_message report a message as sent that never left.
    with pytest.raises(SmtpError, match="not connected to smtp.example.com:465"):
        session.send_raw("me@example.com", ["you@example.com"], b"raw")


def test_login_raises_when_never_connected() -> None:
    session = SmtpSession("smtp.example.com", 587, "starttls")

    with pytest.raises(SmtpError, match="not connected to smtp.example.com:587"):
        session.login("me@example.com", "hunter2")


def test_quit_is_safe_when_never_connected() -> None:
    # Teardown runs from a `finally:` on every send path, so it has to tolerate
    # a session that never opened rather than mask the real error.
    SmtpSession("smtp.example.com", 465).quit()


def test_send_raw_names_the_recipients_in_the_error(monkeypatch) -> None:
    # Arrange: connect() succeeds against a server that then refuses the mail.
    class RefusingSmtp:
        def sendmail(self, from_addr: str, recipients: list[str], raw: bytes) -> None:
            raise smtplib.SMTPRecipientsRefused({})

    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda host, port, timeout: RefusingSmtp())
    session = SmtpSession("smtp.example.com", 465)
    session.connect()

    # Act / Assert: the message says which recipient it failed for.
    with pytest.raises(SmtpError, match="could not send to you@example.com"):
        session.send_raw("me@example.com", ["you@example.com"], b"raw")
