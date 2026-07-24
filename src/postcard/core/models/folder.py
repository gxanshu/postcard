# folder.py
#
# A mailbox folder (Inbox, Sent, ...). The emails inside it are fetched from
# the database on demand — a Folder does not hold them itself.
#
# SPDX-License-Identifier: GPL-3.0-or-later

from gi.repository import GObject


class Folder(GObject.Object):
    __gtype_name__ = "PostcardFolder"

    def __init__(
        self,
        id: int,
        account_id: int,
        name: str,
        icon_name: str,
        parent_id: int | None = None,
        delimiter: str = "/",
    ) -> None:
        super().__init__()
        self.id: int = id
        self.account_id: int = account_id
        self.name: str = name
        self.icon_name: str = icon_name
        self.parent_id: int | None = parent_id
        self.delimiter: str = delimiter
