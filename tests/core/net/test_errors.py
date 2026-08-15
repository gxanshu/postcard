import socket
import ssl

from postcard.core.net import errors
from postcard.core.net.errors import classify
from postcard.core.net.imap_session import ImapError
from postcard.core.net.smtp_session import SmtpError

# Only the "is this the password?" half is asserted — the messages go through
# gettext, so their text depends on the active translation. Everything that is
# not a rejected password is treated the same way by the caller: a banner with
# a Retry button.


def is_auth_failure(exc):
    return classify(exc, "imap.example.com")[0]


def test_a_rejected_password():
    assert is_auth_failure(ImapError("AUTHENTICATIONFAILED: invalid"))
    assert is_auth_failure(SmtpError("5.7.8 Username and Password not accepted"))


def test_a_tls_failure_is_not_a_password_problem():
    assert not is_auth_failure(ssl.SSLError("handshake failure"))


def test_an_unreachable_server_is_not_a_password_problem():
    assert not is_auth_failure(socket.gaierror("Name or service not known"))
    assert not is_auth_failure(ConnectionRefusedError())
    assert not is_auth_failure(TimeoutError())
    assert not is_auth_failure(OSError("network is down"))


def test_any_other_protocol_error_is_passed_through_verbatim():
    assert classify(ImapError("BAD command"), "imap.x") == (False, "BAD command")


def test_an_unrecognised_exception_is_passed_through_verbatim():
    assert classify(ValueError("boom"), "imap.x") == (False, "boom")


def test_a_help_url_becomes_a_link():
    assert errors.linkify("required: https://support.google.com/answer/185833 (x)") == (
        'required: <a href="https://support.google.com/answer/185833">'
        "https://support.google.com/answer/185833</a> (x)"
    )


def test_markup_in_a_server_message_is_escaped_not_rendered():
    assert errors.linkify("<b>A & B</b>") == "&lt;b&gt;A &amp; B&lt;/b&gt;"
