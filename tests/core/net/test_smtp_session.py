import smtplib
import ssl

import pytest

from postcard.core.net.auth import MECHANISM_XOAUTH2, Credential
from postcard.core.net.smtp_session import SmtpError, SmtpSession


class FakeSmtp:
    def __init__(self):
        self.calls = []
        self.greeted = False

    def ehlo_or_helo_if_needed(self):
        self.greeted = True

    def auth(self, mechanism, authobject, initial_response_ok=True):
        self.calls.append(("auth", mechanism, authobject()))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def starttls(self, context=None):
        self.calls.append(("starttls", context))


def connect(monkeypatch, smtp: FakeSmtp) -> SmtpSession:
    monkeypatch.setattr(
        "postcard.core.net.smtp_session.smtplib.SMTP_SSL",
        lambda *args, **kwargs: smtp,
    )
    session = SmtpSession("smtp.example.com", 465)
    session.connect()
    return session


def test_send_raw_raises_when_never_connected() -> None:
    # Arrange: a session that was built but never connect()ed.
    session = SmtpSession("smtp.example.com", 465)

    # Act / Assert: it must fail loudly. Returning normally here would make
    # mail_sync.send_message report a message as sent that never left.
    with pytest.raises(SmtpError, match="not connected to smtp.example.com:465"):
        session.send_raw("me@example.com", ["you@example.com"], b"raw")


def test_sign_in_raises_when_never_connected() -> None:
    session = SmtpSession("smtp.example.com", 587, "starttls")

    with pytest.raises(SmtpError, match="not connected to smtp.example.com:587"):
        session.sign_in(Credential("me@example.com", "hunter2"))


def test_quit_is_safe_when_never_connected() -> None:
    # Teardown runs from a `finally:` on every send path, so it has to tolerate
    # a session that never opened rather than mask the real error.
    SmtpSession("smtp.example.com", 465).quit()


def test_send_raw_names_the_recipients_in_the_error(monkeypatch) -> None:
    # Arrange: connect() succeeds against a server that then refuses the mail.
    class RefusingSmtp:
        def sendmail(self, from_addr: str, recipients: list[str], raw: bytes) -> None:
            raise smtplib.SMTPRecipientsRefused({})

    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *args, **kwargs: RefusingSmtp())
    session = SmtpSession("smtp.example.com", 465)
    session.connect()

    # Act / Assert: the message says which recipient it failed for.
    with pytest.raises(SmtpError, match="could not send to you@example.com"):
        session.send_raw("me@example.com", ["you@example.com"], b"raw")


def test_xoauth2_greets_the_server_before_authenticating(monkeypatch):
    # SMTP_SSL does not EHLO on connect and auth() -- unlike login() -- will not
    # do it either, so the AUTH would go out before the server said hello.
    smtp = FakeSmtp()
    session = connect(monkeypatch, smtp)

    session.sign_in(Credential("me@example.com", "token", MECHANISM_XOAUTH2))

    assert smtp.greeted
    assert smtp.calls == [
        ("auth", "XOAUTH2", "user=me@example.com\x01auth=Bearer token\x01\x01")
    ]


def test_an_unknown_mechanism_fails_rather_than_staying_unauthenticated(monkeypatch):
    smtp = FakeSmtp()
    session = connect(monkeypatch, smtp)

    with pytest.raises(SmtpError, match="unsupported mechanism none"):
        session.sign_in(Credential("me@example.com", "", "none"))

    assert smtp.calls == []


def test_a_password_account_still_uses_plain_login(monkeypatch):
    smtp = FakeSmtp()
    session = connect(monkeypatch, smtp)

    session.sign_in(Credential("me@example.com", "hunter2"))

    assert smtp.calls == [("login", "me@example.com", "hunter2")]


# --- TLS -------------------------------------------------------------------
# Same defect as imaplib: smtplib's implicit context checks neither the
# certificate nor the hostname, so a send would leak the password.


def _assert_verifies(context: ssl.SSLContext) -> None:
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_connect_gives_smtp_ssl_a_verifying_context(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "postcard.core.net.smtp_session.smtplib.SMTP_SSL",
        lambda *args, **kwargs: captured.update(kwargs) or FakeSmtp(),
    )

    SmtpSession("smtp.example.com", 465).connect()

    _assert_verifies(captured["context"])


def test_connect_gives_starttls_a_verifying_context(monkeypatch):
    smtp = FakeSmtp()
    monkeypatch.setattr(
        "postcard.core.net.smtp_session.smtplib.SMTP",
        lambda *args, **kwargs: smtp,
    )

    SmtpSession("smtp.example.com", 587, "starttls").connect()

    assert smtp.calls[0][0] == "starttls"
    _assert_verifies(smtp.calls[0][1])
