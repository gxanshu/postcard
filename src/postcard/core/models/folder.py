from enum import StrEnum

from gi.repository import GObject


class FolderRole(StrEnum):
    """What a mailbox is for.

    A StrEnum so it compares and persists as the plain lowercase string it
    always was -- the database column and the icon table store these values.
    IMAP infers it from the mailbox name (mail_sync.role_for_folder); Microsoft
    Graph says so outright, which is the only way to tell a localized
    "Gesendete Elemente" is the sent folder.
    """

    INBOX = "inbox"
    SENT = "sent"
    DRAFTS = "drafts"
    TRASH = "trash"
    JUNK = "junk"
    ARCHIVE = "archive"
    STARRED = "starred"
    OTHER = "other"


class Folder(GObject.Object):
    __gtype_name__ = "PostcardFolder"

    def __init__(
        self,
        *,
        id: int,
        account_id: int,
        name: str,
        icon_name: str,
        parent_id: int | None = None,
        delimiter: str = "/",
        role: str = "",
        label: str = "",
    ) -> None:
        super().__init__()
        self.id: int = id
        self.account_id: int = account_id
        # How the server addresses this mailbox: an IMAP mailbox name, or a
        # Graph folder id. Never shown when there is a label.
        self.name: str = name
        self.icon_name: str = icon_name
        self.parent_id: int | None = parent_id
        self.delimiter: str = delimiter
        # A FolderRole value the server stated, or "" to infer it from name.
        self.role: str = role
        # The server's display name, or "" to derive it from name.
        self.label: str = label

    # Strip to the leaf name only when there is a parent row to indent under.
    @property
    def display_delimiter(self) -> str | None:
        return self.delimiter if self.parent_id is not None else None
