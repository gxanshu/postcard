from typing import NamedTuple

from .models.account import SECURITY_STARTTLS, SECURITY_TLS


class ProviderSettings(NamedTuple):
    imap_host: str
    imap_port: int
    imap_security: str
    smtp_host: str
    smtp_port: int
    smtp_security: str


_GMAIL = ProviderSettings(
    "imap.gmail.com", 993, SECURITY_TLS, "smtp.gmail.com", 587, SECURITY_STARTTLS
)
_OUTLOOK = ProviderSettings(
    "outlook.office365.com",
    993,
    SECURITY_TLS,
    "smtp-mail.outlook.com",
    587,
    SECURITY_STARTTLS,
)
_YAHOO = ProviderSettings(
    "imap.mail.yahoo.com",
    993,
    SECURITY_TLS,
    "smtp.mail.yahoo.com",
    465,
    SECURITY_TLS,
)
_ICLOUD = ProviderSettings(
    "imap.mail.me.com", 993, SECURITY_TLS, "smtp.mail.me.com", 587, SECURITY_STARTTLS
)

PROVIDERS: dict[str, ProviderSettings] = {
    "gmail.com": _GMAIL,
    "googlemail.com": _GMAIL,
    "outlook.com": _OUTLOOK,
    "hotmail.com": _OUTLOOK,
    "live.com": _OUTLOOK,
    "msn.com": _OUTLOOK,
    "yahoo.com": _YAHOO,
    "yahoo.co.uk": _YAHOO,
    "yahoo.in": _YAHOO,
    "ymail.com": _YAHOO,
    "aol.com": ProviderSettings(
        "imap.aol.com", 993, SECURITY_TLS, "smtp.aol.com", 465, SECURITY_TLS
    ),
    "icloud.com": _ICLOUD,
    "me.com": _ICLOUD,
    "mac.com": _ICLOUD,
    "fastmail.com": ProviderSettings(
        "imap.fastmail.com", 993, SECURITY_TLS, "smtp.fastmail.com", 465, SECURITY_TLS
    ),
    "zoho.com": ProviderSettings(
        "imap.zoho.com", 993, SECURITY_TLS, "smtp.zoho.com", 465, SECURITY_TLS
    ),
    "yandex.com": ProviderSettings(
        "imap.yandex.com", 993, SECURITY_TLS, "smtp.yandex.com", 465, SECURITY_TLS
    ),
}


def settings_for_email(address: str) -> ProviderSettings | None:
    """The known server settings for an address' domain, or None if unknown."""
    _, at_sign, domain = address.strip().rpartition("@")
    return PROVIDERS.get(domain.casefold()) if at_sign else None
