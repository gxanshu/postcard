from dataclasses import dataclass, field


@dataclass
class MessageHeader:
    """A fetched message's headers, already cleaned up for display.

    Distinct from imap_session.FetchedHeader, which is the raw wire form: the
    sender here is a display name, the date is formatted, and `is_unread` is the
    inverse of the server's \\Seen flag.
    """

    uid: str
    sender: str
    sender_address: str
    subject: str
    date: str
    is_unread: bool
    is_starred: bool = False
    preview: str = ""
    message_id: str = ""
    in_reply_to: str = ""
    references: str = ""
    # every (name, address) pair on the message, for the contacts list
    addresses: list[tuple[str, str]] = field(default_factory=list)
