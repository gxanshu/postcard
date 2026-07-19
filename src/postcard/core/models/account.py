# account.py
#
# A mail account: where to log in, nothing more. No logic lives here.
#
# SPDX-License-Identifier: GPL-3.0-or-later

from gi.repository import GObject


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
