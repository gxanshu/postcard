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
        message_id: str = "",
        in_reply_to: str = "",
        references: str = "",
        conversation_id: int | None = None,
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
        self.message_id: str = message_id
        self.in_reply_to: str = in_reply_to
        self.references: str = references
        self.conversation_id: int | None = conversation_id
