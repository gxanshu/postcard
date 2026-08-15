"""Reading mail accounts out of GNOME Online Accounts.

Spoken over D-Bus rather than through libgoa: Goa-1.0.typelib is not in the
GNOME runtime, and the D-Bus API is the same thing one layer down.
"""

import logging
from typing import NamedTuple

from gi.repository import Gio, GLib

from .models.account import SECURITY_STARTTLS, SECURITY_TLS, parse_port
from .net import NET_TIMEOUT_SECONDS
from .net.auth import MECHANISM_XOAUTH2, Credential

logger = logging.getLogger(__name__)

BUS_NAME = "org.gnome.OnlineAccounts"
OBJECT_PATH = "/org/gnome/OnlineAccounts"

_OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
_ACCOUNT = "org.gnome.OnlineAccounts.Account"
_MAIL = "org.gnome.OnlineAccounts.Mail"
_OAUTH2 = "org.gnome.OnlineAccounts.OAuth2Based"

_IMAP_IMPLICIT_TLS_PORT = 993
_IMAP_PORT = 143
_SMTP_IMPLICIT_TLS_PORT = 465
_SMTP_STARTTLS_PORT = 587
_SMTP_PORT = 25

_TIMEOUT_MS = NET_TIMEOUT_SECONDS * 1000
_PROXY_FLAGS = (
    Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES
    | Gio.DBusProxyFlags.DO_NOT_CONNECT_SIGNALS
)

Properties = dict[str, object]
Interfaces = dict[str, Properties]


class OnlineAccount(NamedTuple):
    goa_id: str
    email: str
    display_name: str
    provider_name: str
    imap_host: str
    imap_port: int
    imap_security: str
    smtp_host: str
    smtp_port: int
    smtp_security: str
    is_mail_supported: bool
    # Only OAuth accounts are imported: an IMAP/SMTP one is the Add Account
    # dialog's job, and going through GNOME would buy nothing but the typing.
    is_oauth2: bool


def split_host_port(value: str, default_port: int) -> tuple[str, int]:
    """GNOME Online Accounts keeps a non-default port inside the host string,
    as "mail.example.com:1143"."""
    host, separator, port = value.rpartition(":")
    if not separator:
        return value, default_port
    parsed = parse_port(port)
    return (host, parsed) if parsed is not None else (value, default_port)


def imap_server(mail: Properties) -> tuple[str, int, str]:
    is_implicit_tls = bool(mail.get("ImapUseSsl"))
    host, port = split_host_port(
        str(mail.get("ImapHost", "")),
        _IMAP_IMPLICIT_TLS_PORT if is_implicit_tls else _IMAP_PORT,
    )
    return host, port, SECURITY_TLS if is_implicit_tls else SECURITY_STARTTLS


def smtp_server(mail: Properties) -> tuple[str, int, str]:
    # ponytail: a server with neither flag set is plaintext, which Account
    # cannot express -- we attempt STARTTLS rather than send credentials in the
    # clear. Add a SECURITY_NONE if a real plaintext relay ever turns up.
    is_implicit_tls = bool(mail.get("SmtpUseSsl"))
    if is_implicit_tls:
        default_port = _SMTP_IMPLICIT_TLS_PORT
    elif mail.get("SmtpUseTls"):
        default_port = _SMTP_STARTTLS_PORT
    else:
        default_port = _SMTP_PORT
    host, port = split_host_port(str(mail.get("SmtpHost", "")), default_port)
    return host, port, SECURITY_TLS if is_implicit_tls else SECURITY_STARTTLS


def mail_accounts() -> list[OnlineAccount]:
    """Every account in GNOME Online Accounts, mail-capable or not.

    Accounts that cannot do IMAP come back with is_mail_supported False rather
    than being dropped, so the caller can say why they are unavailable --
    Microsoft 365 is the common case, since its token only covers the Graph API.
    """
    try:
        objects = _managed_objects()
    except GLib.Error:
        logger.debug("could not list GNOME Online Accounts", exc_info=True)
        return []

    accounts = []
    for interfaces in objects.values():
        account = interfaces.get(_ACCOUNT)
        if account is None or account.get("IsTemporary"):
            continue
        # Mail switched off for this account in Settings: the user already said
        # no, and listing it as "unsupported" would misstate why.
        if account.get("MailDisabled"):
            continue

        mail = interfaces.get(_MAIL, {})
        imap_host, imap_port, imap_security = imap_server(mail)
        smtp_host, smtp_port, smtp_security = smtp_server(mail)
        email = str(mail.get("EmailAddress") or account.get("PresentationIdentity", ""))
        accounts.append(
            OnlineAccount(
                goa_id=str(account.get("Id", "")),
                email=email,
                display_name=str(mail.get("Name") or email.partition("@")[0]),
                provider_name=str(account.get("ProviderName", "")),
                imap_host=imap_host,
                imap_port=imap_port,
                imap_security=imap_security,
                smtp_host=smtp_host,
                smtp_port=smtp_port,
                smtp_security=smtp_security,
                is_mail_supported=bool(imap_host and smtp_host),
                is_oauth2=_OAUTH2 in interfaces,
            )
        )
    return accounts


def credential(goa_id: str) -> Credential | None:
    """The live sign-in for one online account.

    Read on every use rather than copied into the keyring, because an OAuth
    access token expires within the hour.
    """
    try:
        objects = _managed_objects()
    except GLib.Error:
        logger.warning("could not reach GNOME Online Accounts", exc_info=True)
        return None

    match = next(
        (
            (path, interfaces)
            for path, interfaces in objects.items()
            if interfaces.get(_ACCOUNT, {}).get("Id") == goa_id
        ),
        None,
    )
    if match is None:
        logger.warning("online account %s is gone from GNOME Online Accounts", goa_id)
        return None

    path, interfaces = match
    if _OAUTH2 not in interfaces:
        logger.warning("online account %s no longer signs in with OAuth", goa_id)
        return None

    try:
        token = _call(path, _OAUTH2, "GetAccessToken").unpack()[0]
    except GLib.Error:
        logger.warning(
            "could not get an access token for online account %s", goa_id, exc_info=True
        )
        return None

    mail = interfaces.get(_MAIL, {})
    user = str(mail.get("ImapUserName") or mail.get("EmailAddress", ""))
    return Credential(user, token, MECHANISM_XOAUTH2)


def _managed_objects() -> dict[str, Interfaces]:
    reply = _call(OBJECT_PATH, _OBJECT_MANAGER, "GetManagedObjects")
    return reply.unpack()[0]


def _call(
    object_path: str,
    interface: str,
    method: str,
    params: GLib.Variant | None = None,
) -> GLib.Variant:
    proxy = Gio.DBusProxy.new_for_bus_sync(
        Gio.BusType.SESSION,
        _PROXY_FLAGS,
        None,
        BUS_NAME,
        object_path,
        interface,
        None,
    )
    return proxy.call_sync(method, params, Gio.DBusCallFlags.NONE, _TIMEOUT_MS, None)
