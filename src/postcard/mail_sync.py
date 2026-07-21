from dataclasses import dataclass, field
from email.utils import parseaddr, parsedate_to_datetime

from .core.models.account import Account
from .core.net.imap_session import ImapSession

# how many recent messages to pull per sync
RECENT_LIMIT = 50


@dataclass
class MessageHeader:
    uid: str
    sender: str
    subject: str
    date: str
    unread: bool
    starred: bool = False
    preview: str = ""
    message_id: str = ""
    in_reply_to: str = ""
    references: str = ""


@dataclass
class SyncResult:
    folders: list[str] = field(default_factory=list)
    messages: list[MessageHeader] = field(default_factory=list)
    folder: str = "INBOX"
    exists: int = 0  # total messages in the selected mailbox
    offset: int = 0  # how far back from the newest this fetch reached


def inbox_name(folders: list[str]) -> str:
    """The server's inbox mailbox. IMAP calls it INBOX but servers vary the
    casing (Yahoo lists it as "Inbox"), so match by role and fall back to the
    canonical name."""
    for name in folders:
        if role_for_folder(name) == "inbox":
            return name
    return "INBOX"


def fetch_mailbox(
    account: Account,
    password: str,
    folder: str | None = None,
    limit: int = RECENT_LIMIT,
    offset: int = 0,
) -> SyncResult:
    """Connect, log in, and return the folder list + recent headers.

    `folder` selects which mailbox to pull headers from; None means the inbox.
    `offset` pages backwards: 0 is the newest `limit`, `limit` is the page
    before that (used to load older mail on scroll).
    """
    session = ImapSession(account.imap_host, account.imap_port, account.imap_security)
    session.connect()

    try:
        session.login(account.email, password)
        folders = session.list_folders()
        target = folder or inbox_name(folders)
        exists = session.select(target)
        raw = session.fetch_recent_headers(exists, limit, offset)
    finally:
        session.logout()

    messages = [
        MessageHeader(
            uid=item["uid"],
            sender=_clean_sender(item["from"]),
            subject=item["subject"] or "(no subject)",
            date=_format_date(item["date"]),
            unread=not item["seen"],
            starred=item["flagged"],
            message_id=item["message_id"],
            in_reply_to=item["in_reply_to"],
            references=item["references"],
        )
        for item in raw
    ]

    return SyncResult(folders, messages, target, exists, offset)


def fetch_full_message(
    account: Account, password: str, folder_name: str, uid: str
) -> bytes:
    """Connect, login, open one folder, and download a single full message"""
    session = ImapSession(account.imap_host, account.imap_port, account.imap_security)
    session.connect()

    try:
        session.login(account.email, password)
        session.select(folder_name)
        return session.fetch_message(uid)
    finally:
        session.logout()


def set_flag(
    account: Account,
    password: str,
    folder_name: str,
    uids: list[str],
    flag: str,
    add: bool,
) -> None:
    """Add or remove an IMAP flag on every message in a conversation."""
    session = ImapSession(account.imap_host, account.imap_port, account.imap_security)
    session.connect()
    try:
        session.login(account.email, password)
        session.select(folder_name, readonly=False)
        for uid in uids:
            session.store_flags(uid, flag, add)
    finally:
        session.logout()


def move_messages(
    account: Account,
    password: str,
    folder_name: str,
    uids: list[str],
    destination: str,
) -> None:
    """Move every message in a conversation to another mailbox."""
    session = ImapSession(account.imap_host, account.imap_port, account.imap_security)
    session.connect()
    try:
        session.login(account.email, password)
        session.select(folder_name, readonly=False)
        for uid in uids:
            session.move(uid, destination)
    finally:
        session.logout()


def role_for_folder(name: str) -> str:
    """Classify a mailbox by name: inbox/sent/drafts/trash/junk/archive/other."""
    lname = name.lower()
    if lname == "inbox":
        return "inbox"
    if "sent" in lname:
        return "sent"
    if "draft" in lname:
        return "drafts"
    if "trash" in lname or "deleted" in lname:
        return "trash"
    if "junk" in lname or "spam" in lname:
        return "junk"
    if "archive" in lname or "all mail" in lname:
        return "archive"
    if "star" in lname or "flagged" in lname:
        return "starred"
    return "other"


def display_name_for_folder(name: str) -> str:
    for prefix in ("[Gmail]/", "[Google Mail]/"):
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def icon_for_folder(name: str) -> str:
    """Pick a symbolic icon name for a mailbox (used in the sidebar).

    Only names that ship in the GNOME runtime's Adwaita icon theme are used;
    mail-inbox/sent/drafts-symbolic are *not* in it and render as broken images.
    """
    return {
        "inbox": "mail-unread-symbolic",
        "sent": "mail-send-symbolic",
        "drafts": "document-edit-symbolic",
        "trash": "user-trash-symbolic",
        "junk": "mail-mark-junk-symbolic",
        "starred": "starred-symbolic",
    }.get(role_for_folder(name), "folder-symbolic")


def _clean_sender(value: str) -> str:
    # "Ada Lovelace <ada@example.com>" -> "Ada Lovelace"; a bare address stays.
    name, addr = parseaddr(value)
    return name or addr or value


def _format_date(value: str) -> str:
    # Turn "Wed, 16 Jul 2026 10:00:00 +0000" into a short "Jul 16".
    try:
        return parsedate_to_datetime(value).strftime("%b %d")
    except (TypeError, ValueError):
        return value
