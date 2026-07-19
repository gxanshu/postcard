from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

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
    preview: str = ""


@dataclass
class SyncResult:
    folders: list[str] = field(default_factory=list)
    messages: list[MessageHeader] = field(default_factory=list)


def fetch_mailbox(
    account: Account, password: str, limit: int = RECENT_LIMIT
) -> SyncResult:
    """Connect, log in, and return the folder list + recent INBOX headers"""
    session = ImapSession(account.imap_host, account.imap_port)
    session.connect()

    try:
        session.login(account.email, password)
        folders = session.list_folders()
        exists = session.select("INBOX")
        raw = session.fetch_recent_headers(exists, limit)
    finally:
        session.logout()

    messages = [
        MessageHeader(
            uid=item["uid"],
            sender=_clean_sender(item["from"]),
            subject=item["subject"] or "(no subject)",
            date=_format_date(item["date"]),
            unread=not item["seen"],
        )
        for item in raw
    ]

    return SyncResult(folders, messages)


def fetch_full_message(
    account: Account, password: str, folder_name: str, uid: str
) -> bytes:
    """Connect, login, open one folder, and download a single full message"""
    session = ImapSession(account.imap_host, account.imap_port)
    session.connect()

    try:
        session.login(account.email, password)
        session.select(folder_name)
        return session.fetch_message(uid)
    finally:
        session.logout()


def icon_for_folder(name: str) -> str:
    """Pick a symbolic icon name for a mailbox (used in the sidebar)."""
    lname = name.lower()
    if lname == "inbox":
        return "mail-inbox-symbolic"
    if "sent" in lname:
        return "mail-sent-symbolic"
    if "draft" in lname:
        return "mail-drafts-symbolic"
    if "trash" in lname or "deleted" in lname:
        return "user-trash-symbolic"
    if "junk" in lname or "spam" in lname:
        return "mail-mark-junk-symbolic"
    return "folder-symbolic"


def _clean_sender(value: str) -> str:
    # "Ada Lovelace <ada@example.com>" -> "Ada Lovelace"; a bare address stays.
    value = value.strip()
    if "<" in value:
        name = value.split("<", 1)[0].strip().strip('"')
        if name:
            return name
    return value


def _format_date(value: str) -> str:
    # Turn "Wed, 16 Jul 2026 10:00:00 +0000" into a short "Jul 16".
    try:
        return parsedate_to_datetime(value).strftime("%b %d")
    except (TypeError, ValueError):
        return value
