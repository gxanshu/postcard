import pytest

from postcard.core.goa import (
    imap_server,
    online_account,
    smtp_server,
    split_host_port,
)
from postcard.core.models.account import (
    GRAPH_HOST,
    PROTOCOL_GRAPH,
    PROTOCOL_IMAP,
    SECURITY_STARTTLS,
    SECURITY_TLS,
)

# --- split_host_port --------------------------------------------------------


def test_a_port_written_into_the_host_string_wins_over_the_default():
    assert split_host_port("mail.example.com:1143", 993) == ("mail.example.com", 1143)


def test_a_bare_host_keeps_the_default_port():
    assert split_host_port("mail.example.com", 993) == ("mail.example.com", 993)


@pytest.mark.parametrize("value", ["mail.example.com:", "mail.example.com:0"])
def test_an_unusable_port_falls_back_rather_than_truncating_the_host(value):
    # Splitting on the colon regardless would leave the host trailing a ":" or
    # dial port 0, which is not routable.
    assert split_host_port(value, 993) == (value, 993)


# --- security and default ports ---------------------------------------------


def test_imap_over_implicit_tls_uses_993():
    assert imap_server({"ImapHost": "imap.example.com", "ImapUseSsl": True}) == (
        "imap.example.com",
        993,
        SECURITY_TLS,
    )


def test_imap_that_negotiates_up_uses_143():
    assert imap_server({"ImapHost": "imap.example.com", "ImapUseTls": True}) == (
        "imap.example.com",
        143,
        SECURITY_STARTTLS,
    )


@pytest.mark.parametrize(
    ("mail", "expected_port"),
    [
        ({"SmtpUseSsl": True}, 465),
        ({"SmtpUseTls": True}, 587),
        ({}, 25),
    ],
)
def test_smtp_default_port_follows_the_encryption_flags(mail, expected_port):
    _host, port, _security = smtp_server({"SmtpHost": "smtp.example.com", **mail})

    assert port == expected_port


def test_a_mailless_account_reports_no_host():
    # Microsoft 365 and Exchange expose no IMAP server at all; the caller uses
    # the empty host to mark them unavailable rather than crashing on it.
    assert imap_server({})[0] == ""


# --- online_account ---------------------------------------------------------

_ACCOUNT = "org.gnome.OnlineAccounts.Account"
_MAIL = "org.gnome.OnlineAccounts.Mail"
_OAUTH2 = "org.gnome.OnlineAccounts.OAuth2Based"


def test_a_microsoft_365_account_is_reached_over_graph():
    account = {
        "Id": "account_2",
        "ProviderType": "ms_graph",
        "ProviderName": "Microsoft 365",
    }
    # GOA lists Mail for it, but with IMAP and SMTP switched off.
    mail = {"EmailAddress": "ada@contoso.com", "ImapSupported": False, "ImapHost": ""}

    online = online_account(account, {_ACCOUNT: account, _MAIL: mail, _OAUTH2: {}})

    assert online.protocol == PROTOCOL_GRAPH
    assert online.is_mail_supported
    assert online.is_oauth2
    assert (online.imap_host, online.smtp_host) == (GRAPH_HOST, GRAPH_HOST)
    assert online.email == "ada@contoso.com"


def test_a_microsoft_account_without_mail_is_not_offered():
    account = {"Id": "account_3", "ProviderType": "ms_graph"}

    online = online_account(account, {_ACCOUNT: account, _OAUTH2: {}})

    assert not online.is_mail_supported


def test_an_exchange_account_has_no_mail_postcard_can_read():
    account = {
        "Id": "account_4",
        "ProviderType": "exchange",
        "PresentationIdentity": "a@x",
    }

    online = online_account(account, {_ACCOUNT: account, _MAIL: {}})

    assert online.protocol == PROTOCOL_IMAP
    assert not online.is_mail_supported


def test_a_google_account_stays_on_imap():
    account = {"Id": "account_1", "ProviderType": "google"}
    mail = {
        "EmailAddress": "ada@gmail.com",
        "ImapHost": "imap.gmail.com",
        "ImapUseSsl": True,
        "SmtpHost": "smtp.gmail.com",
        "SmtpUseSsl": True,
    }

    online = online_account(account, {_ACCOUNT: account, _MAIL: mail, _OAUTH2: {}})

    assert online.protocol == PROTOCOL_IMAP
    assert (online.imap_host, online.imap_port) == ("imap.gmail.com", 993)
    assert online.is_mail_supported
