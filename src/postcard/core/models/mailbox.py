from typing import NamedTuple


class MailboxInfo(NamedTuple):
    """One mailbox as a server lists it, before it becomes a Folder row.

    IMAP fills only the first three: the role and the display name are inferred
    from the name, and the parent is found by splitting it on the delimiter.
    Graph addresses folders by opaque id, so it states all three outright.
    """

    name: str
    delimiter: str  # "" when the server reports NIL: a flat namespace
    flags: str
    label: str = ""
    role: str = ""  # a FolderRole value
    # The parent mailbox's name, "" at the top level; None to split name.
    parent: str | None = None
