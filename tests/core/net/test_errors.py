import socket
import ssl

from postcard.core.net import errors
from postcard.core.net.errors import classify
from postcard.core.net.imap_session import ImapError
from postcard.core.net.smtp_session import SmtpError

# Only the category is asserted — the messages go through gettext, so their
# text depends on the active translation.


def category(exc):
    return classify(exc, "imap.example.com")[0]


def test_a_tls_failure():
    assert category(ssl.SSLError("handshake failure")) == "tls"


def test_an_unresolvable_host():
    assert category(socket.gaierror("Name or service not known")) == "unreachable"


def test_a_refused_connection():
    assert category(ConnectionRefusedError()) == "unreachable"


def test_a_timeout():
    assert category(TimeoutError()) == "unreachable"


def test_a_rejected_password():
    assert category(ImapError("AUTHENTICATIONFAILED: invalid")) == "auth"
    assert category(SmtpError("5.7.8 Username and Password not accepted")) == "auth"


def test_any_other_protocol_error_is_the_server_s_fault():
    assert classify(ImapError("BAD command"), "imap.x") == ("server", "BAD command")


def test_a_generic_socket_error_is_treated_as_unreachable():
    assert category(OSError("network is down")) == "unreachable"


def test_an_unrecognised_exception_falls_back_to_server():
    assert classify(ValueError("boom"), "imap.x") == ("server", "boom")


def test_a_help_url_becomes_a_link():
    assert errors.linkify("required: https://support.google.com/answer/185833 (x)") == (
        'required: <a href="https://support.google.com/answer/185833">'
        "https://support.google.com/answer/185833</a> (x)"
    )


def test_markup_in_a_server_message_is_escaped_not_rendered():
    assert errors.linkify("<b>A & B</b>") == "&lt;b&gt;A &amp; B&lt;/b&gt;"
