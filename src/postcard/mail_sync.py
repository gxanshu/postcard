from dataclasses import dataclass, field
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from enum import StrEnum

from .core.models.account import Account
from .core.models.conversation import Conversation
from .core.net.imap_session import ImapSession, MailboxInfo, decode_mailbox_name
from .core.net.smtp_session import SmtpSession

# how many recent messages to pull per sync
RECENT_LIMIT = 50

# Gmail nests its special folders under an unselectable "[Gmail]" container.
# It isn't a real mailbox, so it's hidden and its children sit at the top level.
NAMESPACE_ROOTS = ("[Gmail]", "[Google Mail]")


class FolderRole(StrEnum):
    """What a mailbox is for, inferred from its name by role_for_folder.

    A StrEnum so it compares and persists as the plain lowercase string it
    always was -- the database column and the icon table are unchanged.
    """

    INBOX = "inbox"
    SENT = "sent"
    DRAFTS = "drafts"
    TRASH = "trash"
    JUNK = "junk"
    ARCHIVE = "archive"
    STARRED = "starred"
    OTHER = "other"


# The canonical IMAP inbox name. Servers vary the casing, so this is the
# fallback rather than something to compare against -- see inbox_name.
INBOX_MAILBOX = "INBOX"

# Folders Postcard maintains itself rather than mirroring from the server.
# Outbox is local-only (prune_folders keeps it); Sent and Drafts are created on
# demand when the server has no folder of that role.
OUTBOX_FOLDER = "Outbox"
SENT_FOLDER = "Sent"
DRAFTS_FOLDER = "Drafts"


@dataclass
class MessageHeader:
    uid: str
    sender: str
    sender_address: str
    subject: str
    date: str
    unread: bool
    starred: bool = False
    preview: str = ""
    message_id: str = ""
    in_reply_to: str = ""
    references: str = ""
    # every (name, address) pair on the message, for the contacts list
    addresses: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class SyncResult:
    folders: list[MailboxInfo] = field(default_factory=list)
    messages: list[MessageHeader] = field(default_factory=list)
    folder: str = INBOX_MAILBOX
    exists: int = 0  # total messages in the selected mailbox
    offset: int = 0  # how far back from the newest this fetch reached
    all_uids: set[str] | None = None  # authoritative UID snapshot for newest page


@dataclass
class MoveResult:
    """Results from the commands attempted by a mailbox move."""

    destination_uids: list[str | None] = field(default_factory=list)
    failed_index: int | None = None
    error: str | None = None


def inbox_name(folders: list[str]) -> str:
    """The server's inbox mailbox. IMAP calls it INBOX but servers vary the
    casing (Yahoo lists it as "Inbox"), so match by role and fall back to the
    canonical name."""
    for name in folders:
        if role_for_folder(name) == FolderRole.INBOX:
            return name
    return INBOX_MAILBOX


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
        mailboxes = session.list_folders()
        target = folder or inbox_name([m.name for m in mailboxes])
        exists = session.select(target)
        all_uids = session.search_all_uids() if offset == 0 else None
        raw = session.fetch_recent_headers(exists, limit, offset)
    finally:
        session.logout()

    messages = [
        MessageHeader(
            uid=item["uid"],
            sender=_clean_sender(item["from"]),
            sender_address=_sender_address(item["from"]),
            subject=item["subject"] or "(no subject)",
            date=_format_date(item["date"]),
            unread=not item["seen"],
            starred=item["flagged"],
            message_id=item["message_id"],
            in_reply_to=item["in_reply_to"],
            references=item["references"],
            addresses=getaddresses([item["from"], item["to"], item["cc"]]),
        )
        for item in raw
    ]

    return SyncResult(
        folders=mailboxes,
        messages=messages,
        folder=target,
        exists=exists,
        offset=offset,
        all_uids=all_uids,
    )


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


def server_uids(conversation: Conversation) -> list[str]:
    """The IMAP UIDs of a conversation's messages, skipping any without one.

    A locally saved copy (a Sent message, before the next sync confirms it)
    has no UID, and imaplib silently drops a None argument -- which would send
    a UID-less "UID STORE +FLAGS (...)" and get a BAD back. There is nothing
    on the server to act on yet, so leave those out.
    """
    return [mail.server_id for mail in conversation.emails if mail.server_id]


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
) -> MoveResult:
    """Move every message in a conversation to another mailbox."""
    session = ImapSession(account.imap_host, account.imap_port, account.imap_security)
    session.connect()
    try:
        session.login(account.email, password)
        session.select(folder_name, readonly=False)
        destination_uids = []
        for index, uid in enumerate(uids):
            try:
                destination_uids.append(session.move(uid, destination))
            except Exception as error:
                return MoveResult(destination_uids, index, str(error))
    finally:
        session.logout()
    return MoveResult(destination_uids)


def send_message(
    account: Account, password: str, from_addr: str, recipients: list[str], raw: bytes
) -> None:
    """Connect, log in, and hand a fully-built message to the server."""
    session = SmtpSession(account.smtp_host, account.smtp_port, account.smtp_security)
    session.connect()

    try:
        session.login(account.email, password)
        session.send_raw(from_addr, recipients, raw)
    finally:
        session.quit()


def role_for_folder(name: str) -> FolderRole:  # noqa: PLR0911
    """Classify a mailbox by name.

    Matched by substring and tolerant of casing, because servers name these
    differently ("Deleted Items", "[Gmail]/All Mail", "Bulk Mail").

    noqa PLR0911: a dispatch table — one return per role is the point.
    """
    lowered = name.lower()
    if lowered == FolderRole.INBOX:
        return FolderRole.INBOX
    if "sent" in lowered:
        return FolderRole.SENT
    if "draft" in lowered:
        return FolderRole.DRAFTS
    if "trash" in lowered or "deleted" in lowered:
        return FolderRole.TRASH
    if "junk" in lowered or "spam" in lowered:
        return FolderRole.JUNK
    if "archive" in lowered or "all mail" in lowered:
        return FolderRole.ARCHIVE
    if "star" in lowered or "flagged" in lowered:
        return FolderRole.STARRED
    return FolderRole.OTHER


def parent_mailbox_name(name: str, delimiter: str) -> str:
    """The mailbox enclosing name, or "" when it sits at the top level."""
    parent = name.rpartition(delimiter)[0] if delimiter else ""
    return "" if parent in NAMESPACE_ROOTS else parent


def display_name_for_folder(name: str, delimiter: str | None = None) -> str:
    name = decode_mailbox_name(name)
    for root in NAMESPACE_ROOTS:
        if name.startswith(root + "/"):
            name = name[len(root) + 1 :]
            break
    if delimiter:
        name = name.rsplit(delimiter, 1)[-1]
    return name


def icon_for_folder(name: str) -> str:
    """Pick a symbolic icon name for a mailbox (used in the sidebar).

    Only names that ship in the GNOME runtime's Adwaita icon theme are used;
    mail-inbox/sent/drafts-symbolic are *not* in it and render as broken images.
    """
    return {
        FolderRole.INBOX: "mail-unread-symbolic",
        FolderRole.SENT: "mail-send-symbolic",
        FolderRole.DRAFTS: "document-edit-symbolic",
        FolderRole.TRASH: "user-trash-symbolic",
        FolderRole.JUNK: "mail-mark-junk-symbolic",
        FolderRole.STARRED: "starred-symbolic",
    }.get(role_for_folder(name), "folder-symbolic")


def _clean_sender(value: str) -> str:
    # "Ada Lovelace <ada@example.com>" -> "Ada Lovelace"; a bare address stays.
    name, addr = parseaddr(value)
    return name or addr or value


def _sender_address(value: str) -> str:
    # "Ada Lovelace <ada@example.com>" -> "ada@example.com", "" if unparseable.
    return parseaddr(value)[1].strip().lower()


def _format_date(value: str) -> str:
    # Turn "Wed, 16 Jul 2026 10:00:00 +0000" into a short "Jul 16".
    try:
        return parsedate_to_datetime(value).strftime("%b %d")
    except (TypeError, ValueError):
        return value
