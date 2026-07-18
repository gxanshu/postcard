# email.py
#
# A single email message (headers + preview only, for now — the full body
# arrives in Phase 6).
#
# SPDX-License-Identifier: GPL-3.0-or-later

from gi.repository import GObject


class Email(GObject.Object):
    __gtype_name__ = "PostboxEmail"

    def __init__(
        self,
        id: int,
        folder_id: int,
        server_id: str,
        sender: str,
        subject: str,
        preview: str,
        date: str,
        unread: bool,
    ) -> None:
        super().__init__()
        self.id: int = id
        self.folder_id: int = folder_id
        self.server_id: str = server_id
        self.sender: str = sender
        self.subject: str = subject
        self.preview: str = preview
        self.date: str = date
        self.unread: bool = unread
