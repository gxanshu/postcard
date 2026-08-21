import logging
import re
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from enum import StrEnum
from gettext import gettext as _

from .core.models.account import Account
from .core.models.conversation import Conversation
from .core.models.folder import Folder

# Re-exported: callers reach MessageHeader through mail_sync, which is where it
# is built. It lives in core.models so core.store can accept one directly.
from .core.models.message_header import MessageHeader
from .core.net.auth import Credential
from .core.net.imap_session import (
    ATTR_NOSELECT,
    GMAIL_CAPABILITY,
    FetchedHeader,
    ImapSession,
    MailboxInfo,
    decode_mailbox_name,
)
from .core.net.smtp_session import SmtpSession
from .core.store.database import Database
from .core.threader import NO_SUBJECT

logger = logging.getLogger(__name__)

# how many recent messages to pull per sync
RECENT_LIMIT = 50

# RFC 8058: the body is the whole request, and the server matches it verbatim.
ONE_CLICK_BODY = b"List-Unsubscribe=One-Click"
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
UNSUBSCRIBE_TIMEOUT_SECONDS = 15

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


# How servers spell each role, in the order role_for_folder tries them: the
# first match wins, so "Sent/Drafts" is a sent mailbox rather than a drafts one.
# Word boundaries rather than a bare substring, so a mailbox the user named
# "Consent forms", "Presentations" or "Unsent Messages" isn't taken for Sent.
_ROLE_PATTERNS: tuple[tuple[re.Pattern[str], FolderRole], ...] = (
    (re.compile(r"^inbox$", re.IGNORECASE), FolderRole.INBOX),
    (re.compile(r"\bsent\b", re.IGNORECASE), FolderRole.SENT),
    (re.compile(r"\bdrafts?\b", re.IGNORECASE), FolderRole.DRAFTS),
    (re.compile(r"\b(trash|deleted)\b", re.IGNORECASE), FolderRole.TRASH),
    (re.compile(r"\b(junk|spam)\b", re.IGNORECASE), FolderRole.JUNK),
    (re.compile(r"\b(archives?|all mail)\b", re.IGNORECASE), FolderRole.ARCHIVE),
    (re.compile(r"\b(starred|flagged)\b", re.IGNORECASE), FolderRole.STARRED),
)

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
class SyncResult:
    folders: list[MailboxInfo] = field(default_factory=list)
    messages: list[MessageHeader] = field(default_factory=list)
    folder: str = INBOX_MAILBOX
    exists: int = 0  # total messages in the selected mailbox
    offset: int = 0  # how far back from the newest this fetch reached
    all_uids: set[str] | None = None  # authoritative UID snapshot for newest page
    unread_counts: dict[str, int] = field(default_factory=dict)  # by mailbox name


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
    return mailbox_with_role(folders, FolderRole.INBOX) or INBOX_MAILBOX


def fetch_mailbox(
    account: Account,
    credential: Credential,
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
        session.sign_in(credential)
        mailboxes = session.list_folders()
        target = folder or inbox_name([m.name for m in mailboxes])
        exists = session.select(target)
        all_uids = session.search_all_uids() if offset == 0 else None
        raw = session.fetch_recent_headers(exists, limit, offset)
        counts = _unread_counts(session, mailboxes, target) if offset == 0 else {}
    finally:
        session.logout()

    messages = [_to_message_header(fetched) for fetched in raw]

    return SyncResult(
        folders=mailboxes,
        messages=messages,
        folder=target,
        exists=exists,
        offset=offset,
        all_uids=all_uids,
        unread_counts=counts,
    )


def _unread_counts(
    session: ImapSession, mailboxes: list[MailboxInfo], target: str
) -> dict[str, int]:
    """Server unread counts for the role folders this sync didn't fetch.

    Only the target mailbox is fetched, so without this every other folder's
    badge stays at whatever the last visit left behind. A folder that won't
    answer is skipped rather than failing the sync -- the counts are a garnish.
    """
    counts: dict[str, int] = {}
    for mailbox in mailboxes:
        if mailbox.name == target or ATTR_NOSELECT in mailbox.flags:
            continue
        if role_for_folder(mailbox.name) is FolderRole.OTHER:
            continue
        try:
            counts[mailbox.name] = session.unseen_count(mailbox.name)
        except Exception:
            logger.warning(
                "could not read the unread count of %s", mailbox.name, exc_info=True
            )
    return counts


def _to_message_header(fetched: FetchedHeader) -> MessageHeader:
    """Turn raw wire headers into the display-ready form.

    This is where the two shapes differ: the sender becomes a display name,
    the date is normalized to a timestamp, and the server's \\Seen flag is
    inverted into `is_unread`, which is how the rest of the app thinks about it.
    """
    recipient_name, recipient_address = first_recipient(fetched.to_header)
    return MessageHeader(
        uid=fetched.uid,
        sender=_clean_sender(fetched.from_header),
        sender_address=_sender_address(fetched.from_header),
        recipient=recipient_name,
        recipient_address=recipient_address,
        subject=fetched.subject or NO_SUBJECT,
        date=_iso_date(fetched.date),
        is_unread=not fetched.seen,
        is_starred=fetched.flagged,
        message_id=fetched.message_id,
        in_reply_to=fetched.in_reply_to,
        references=fetched.references,
        addresses=getaddresses(
            [fetched.from_header, fetched.to_header, fetched.cc_header]
        ),
    )


def fetch_full_message(
    account: Account, credential: Credential, folder_name: str, uid: str
) -> bytes:
    """Connect, login, open one folder, and download a single full message"""
    session = ImapSession(account.imap_host, account.imap_port, account.imap_security)
    session.connect()

    try:
        session.sign_in(credential)
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
    credential: Credential,
    folder_name: str,
    uids: Sequence[str],
    flag: str,
    should_add: bool,
) -> None:
    """Add or remove an IMAP flag on every message in a conversation.

    One STORE for the whole UID set rather than one per message, which made
    reading a long thread a round trip per message in it. An empty set is not
    an empty command but a malformed one, and a conversation can be entirely
    locally saved copies that have no UID yet -- see server_uids.
    """
    if not uids:
        return
    session = ImapSession(account.imap_host, account.imap_port, account.imap_security)
    session.connect()
    try:
        session.sign_in(credential)
        session.select(folder_name, is_readonly=False)
        session.store_flags(",".join(uids), flag, should_add)
    finally:
        session.logout()


def move_messages(
    account: Account,
    credential: Credential,
    folder_name: str,
    uids: list[str],
    destination: str,
) -> MoveResult:
    """Move every message in a conversation to another mailbox."""
    session = ImapSession(account.imap_host, account.imap_port, account.imap_security)
    session.connect()
    try:
        session.sign_in(credential)
        session.select(folder_name, is_readonly=False)
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
    account: Account,
    credential: Credential,
    from_addr: str,
    recipients: list[str],
    raw: bytes,
) -> None:
    """Connect, log in, and hand a fully-built message to the server."""
    session = SmtpSession(account.smtp_host, account.smtp_port, account.smtp_security)
    session.connect()

    try:
        session.sign_in(credential)
        session.send_raw(from_addr, recipients, raw)
    finally:
        session.quit()

    # SMTP only hands the message to the recipient's server; nothing puts a copy
    # in our own Sent mailbox, so without this the mail is missing from every
    # other client. Never fatal: it has already gone out, and failing here would
    # leave it in the Outbox to be sent a second time.
    try:
        _append_to_sent(account, credential, raw)
    except Exception:
        logger.warning(
            "could not save a copy of the sent message to Sent on %s (account %s)",
            account.imap_host,
            account.email,
            exc_info=True,
        )


def post_unsubscribe(url: str) -> None:
    """Send an RFC 8058 one-click unsubscribe. Raises unless the list accepts it.

    urlopen raises HTTPError for any 4xx/5xx, so a return means the list took
    the request. Nothing authenticates this: no cookies, no credentials, no
    identifying User-Agent beyond urllib's own.
    """
    request = urllib.request.Request(
        url,
        data=ONE_CLICK_BODY,
        headers={"Content-Type": FORM_CONTENT_TYPE},
        method="POST",
    )
    urllib.request.urlopen(request, timeout=UNSUBSCRIBE_TIMEOUT_SECONDS).close()


def _append_to_sent(account: Account, credential: Credential, raw: bytes) -> None:
    session = ImapSession(account.imap_host, account.imap_port, account.imap_security)
    session.connect()

    try:
        session.sign_in(credential)
        if session.has_capability(GMAIL_CAPABILITY):
            return
        sent = mailbox_with_role(
            (mailbox.name for mailbox in session.list_folders()), FolderRole.SENT
        )
        if sent is None:
            logger.warning(
                "no Sent mailbox on %s (account %s)", account.imap_host, account.email
            )
            return
        session.append(sent, raw)
    finally:
        session.logout()


def role_for_folder(name: str) -> FolderRole:
    """Classify a mailbox by name.

    Matched by regex and tolerant of casing, because servers name these
    differently ("Sent Items", "[Gmail]/Sent Mail", "Deleted Items").
    """
    for pattern, role in _ROLE_PATTERNS:
        if pattern.search(name):
            return role
    return FolderRole.OTHER


def is_outgoing_folder(name: str) -> bool:
    """Whether a folder holds mail this account sent.

    The sender of everything in one is the account itself, so the list shows
    the recipient instead. Outbox is local-only and role_for_folder, which
    classifies what the server offers, doesn't know it.
    """
    return name == OUTBOX_FOLDER or role_for_folder(name) in (
        FolderRole.SENT,
        FolderRole.DRAFTS,
    )


def first_recipient(to_header: str) -> tuple[str, str]:
    """A To header as (display name, address), both empty if it names nobody.

    parseaddr, which the sender helpers use, returns empty strings for a header
    listing more than one address -- and a sent message usually does.
    """
    name, address = next(iter(getaddresses([to_header])), ("", ""))
    address = address.strip().lower()
    return name or address, address


def mailbox_with_role(names: Iterable[str], role: FolderRole) -> str | None:
    """The mailbox this server uses for a role, or None when it lists none.

    Every provider spells these its own way -- sent mail lives in "Sent",
    "Sent Items", "Sent Messages", "[Gmail]/Sent Mail" or "INBOX.Sent" -- so
    the name has to come from the server's own list, never from a constant.
    """
    return next((name for name in names if role_for_folder(name) is role), None)


def sent_folder(db: Database, account_id: int) -> Folder:
    """The local folder that mirrors this account's sent mailbox on the server.

    Reads and writes the database, so main thread only. Saving the copy under a
    folder literally named "Sent" files it beside the server's own sent mailbox
    instead of in it, and the next sync's prune_folders then drops that folder
    and the copy with it -- the mail vanishes from the app. Falls back to
    creating "Sent" for an account whose folder list hasn't synced yet.
    """
    names = (folder.name for folder in db.folders_for_account(account_id))
    name = mailbox_with_role(names, FolderRole.SENT) or SENT_FOLDER
    return db.get_or_create_folder(account_id, name, icon_for_folder(name))


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
    """Pick a symbolic icon name for a mailbox (used in the sidebar)."""

    return {
        FolderRole.INBOX: "mail-unread-symbolic",
        FolderRole.SENT: "mail-send-symbolic",
        FolderRole.DRAFTS: "document-edit-symbolic",
        FolderRole.ARCHIVE: "mail-archive-symbolic",
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


def _iso_date(value: str) -> str:
    # "Wed, 16 Jul 2026 10:00:00 +0000" -> "2026-07-16T10:00:00+00:00". Stored
    # as a timestamp rather than a label because format_date is relative to
    # today, and the column is written once at sync time and never revisited.
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return value


def format_date(value: str) -> str:
    """Render a stored timestamp as the short label the list and reader show.

    "Today 10:00" for today, "Yesterday", a weekday name within the last week,
    and "Jul 16" beyond it. Anything unparseable is passed through untouched,
    which covers both a message whose Date header we couldn't read and the
    pre-formatted dates rows carried before this column held a timestamp.
    """
    try:
        moment = datetime.fromisoformat(value).astimezone()
    except (TypeError, ValueError):
        return value

    days = (date.today() - moment.date()).days
    if days == 0:
        return _("Today {time}").format(time=moment.strftime("%H:%M"))
    if days == 1:
        return _("Yesterday")
    if 1 < days < 7:
        return moment.strftime("%a")
    return moment.strftime("%b %d")
