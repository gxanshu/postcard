# account.py
#
# A mail account: where to log in, nothing more. No logic lives here.
#
# SPDX-License-Identifier: GPL-3.0-or-later

from gi.repository import GObject

# Connection security values, in the order the account dialog's ComboRows list
# them ("TLS", "STARTTLS") -- get_selected() indexes straight into this.
SECURITY_TLS = "tls"
SECURITY_STARTTLS = "starttls"
SECURITY_OPTIONS = (SECURITY_TLS, SECURITY_STARTTLS)

# TCP ports are 16-bit and 0 is not dialable.
MIN_PORT = 1
MAX_PORT = 65535


def parse_port(text: str) -> int | None:
    """A port number from user input, or None when it isn't one.

    Returns None rather than raising: the caller is validating a text entry, so
    "not a port yet" is an expected state, not an error.
    """
    try:
        port = int(text.strip())
    except ValueError:
        return None
    return port if MIN_PORT <= port <= MAX_PORT else None


class Account(GObject.Object):
    __gtype_name__ = "PostcardAccount"

    def __init__(
        self,
        id: int,
        email: str,
        display_name: str,
        imap_host: str,
        imap_port: int,
        smtp_host: str,
        smtp_port: int,
        imap_security: str = "tls",
        smtp_security: str = "tls",
    ) -> None:
        super().__init__()
        self.id: int = id
        self.email: str = email
        self.display_name: str = display_name
        self.imap_host: str = imap_host
        self.imap_port: int = imap_port
        self.smtp_host: str = smtp_host
        self.smtp_port: int = smtp_port
        self.imap_security: str = imap_security
        self.smtp_security: str = smtp_security
