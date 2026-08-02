import base64
import email
import imaplib
import logging
import re
from email import policy
from typing import NamedTuple

from . import NET_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

# imaplib returns the command status as the first element of every reply.
STATUS_OK = "OK"

# IMAP system flags (RFC 3501 2.3.2). These are the protocol contract: the same
# spellings are parsed out of a FETCH reply here and sent back by store_flags,
# so both halves have to name them from one place.
FLAG_SEEN = "\\Seen"
FLAG_FLAGGED = "\\Flagged"

# A LIST attribute, not a message flag: the mailbox is a container that cannot
# hold mail (Gmail's "[Gmail]"), so it is shown but never selected.
ATTR_NOSELECT = "\\Noselect"

# Gmail files its own copy of everything sent through it. This capability is how
# it identifies itself, so we don't append a second copy on top.
GMAIL_CAPABILITY = "X-GM-EXT-1"


class MailboxInfo(NamedTuple):
    name: str
    delimiter: str  # "" when the server reports NIL: a flat namespace
    flags: str


class FetchedHeader(NamedTuple):
    """One message's headers exactly as the server sent them.

    Raw on purpose: addresses are unparsed header text and `date` is the
    original RFC 5322 string. mail_sync turns this into a MessageHeader, which
    is the display-ready form.
    """

    uid: str
    from_header: str
    to_header: str
    cc_header: str
    subject: str
    date: str
    message_id: str
    in_reply_to: str
    references: str
    seen: bool
    flagged: bool


def decode_mailbox_name(name: str) -> str:
    """Decode a mailbox name from modified UTF-7 (RFC 3501 5.1.3), so
    "Entw&APw-rfe" reads as "Entwürfe"."""

    def chunk(match: re.Match[str]) -> str:
        if not match.group(1):
            return "&"  # "&-" encodes a literal ampersand
        try:
            padded = match.group(1).replace(",", "/") + "==="
            return base64.b64decode(padded).decode("utf-16-be")
        except (ValueError, UnicodeDecodeError):
            return "�"

    return re.sub(r"&([A-Za-z0-9+,]*)-", chunk, name)


def _quote_mailbox(name: str) -> str:
    # imaplib sends mailbox names verbatim, so "[Gmail]/Sent Mail" arrives as
    # two tokens and the server answers BAD. Quote it (escaping \ and ") so the
    # space stays inside one astring, per RFC 3501.
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _unquote(token: str) -> str:
    """The inverse of _quote_mailbox: drop the quotes and backslash escapes."""
    if token.startswith('"') and token.endswith('"'):
        token = token[1:-1]
    return re.sub(r"\\(.)", r"\1", token)


class ImapError(Exception):
    """Raised when talking to the server fails (bad login, dropped link, ..)"""


class ImapSession:
    def __init__(self, host: str, port: int, security: str = "tls") -> None:
        self._host = host
        self._port = port
        self._security = security
        self._imap: imaplib.IMAP4 | None = None

    def connect(self) -> str:
        if self._security == "starttls":
            self._imap = imaplib.IMAP4(
                self._host, self._port, timeout=NET_TIMEOUT_SECONDS
            )
            self._imap.starttls()
        else:
            self._imap = imaplib.IMAP4_SSL(
                self._host, self._port, timeout=NET_TIMEOUT_SECONDS
            )
        return self._imap.welcome.decode("utf-8", "replace")

    def login(self, user: str, password: str) -> None:
        try:
            self._require_imap().login(user, password)
        except imaplib.IMAP4.error as error:
            raise ImapError(str(error)) from error

    def logout(self) -> None:
        # Runs from a `finally:` on every operation, so it must not raise and
        # mask the error that is already on its way out. Logged at debug
        # because a server hanging up first is normal, not a problem.
        try:
            if self._imap is not None:
                self._imap.logout()
        except Exception:
            logger.debug("IMAP logout from %s failed", self._host, exc_info=True)

    def _require_imap(self) -> imaplib.IMAP4:
        # Never return None: a caller that skipped connect() has to fail loudly
        # rather than quietly do nothing and look like it succeeded.
        if self._imap is None:
            raise ImapError(f"not connected to {self._host}:{self._port}")
        return self._imap

    def list_folders(self) -> list[MailboxInfo]:
        """Return every listed mailbox.

        \\Noselect containers are included so the caller can rebuild the
        hierarchy. The delimiter is "" when the server reports NIL, meaning a
        flat namespace whose names must not be split into parent and child.
        """
        status, payload = self._require_imap().list()
        result: list[MailboxInfo] = []
        for raw in payload:
            if not isinstance(raw, bytes):
                continue
            line = raw.decode("utf-8", "replace")
            match = re.match(r'\(([^)]*)\) ("[^"]*"|NIL) (.+)$', line)
            if match is None:
                continue
            flags_part = match.group(1)
            delim_raw = match.group(2)
            name = _unquote(match.group(3).strip())
            delimiter = "" if delim_raw == "NIL" else _unquote(delim_raw)
            result.append(MailboxInfo(name, delimiter, flags_part))
        return result

    def select(self, mailbox: str, is_readonly: bool = True) -> int:
        """Open a mailbox; return how many messages it holds.

        is_readonly=True (the default) keeps us non-destructive and never marks
        mail as read. Flag/move actions open it writable.
        """
        status, payload = self._require_imap().select(
            _quote_mailbox(mailbox), readonly=is_readonly
        )
        if status != STATUS_OK:
            raise ImapError(f"could not open {mailbox}: {payload}")
        return int(payload[0]) if payload and payload[0] else 0

    def has_capability(self, name: str) -> bool:
        """Whether the server advertises a capability. imaplib upper-cases the
        ones it parsed, so the comparison has to as well."""
        return name.upper() in self._require_imap().capabilities

    def append(self, mailbox: str, raw: bytes) -> None:
        """Upload a message into a mailbox, without selecting it first.

        Stored \\Seen: this is our own copy of something we just sent, and
        arriving as unread mail would be wrong.
        """
        status, payload = self._require_imap().append(
            _quote_mailbox(mailbox), FLAG_SEEN, None, raw
        )
        if status != STATUS_OK:
            raise ImapError(f"could not append to {mailbox}: {payload}")

    def store_flags(self, uid: str, flags: str, should_add: bool) -> None:
        """Add or remove flags (e.g. "\\Seen") on one message by UID."""
        command = "+FLAGS" if should_add else "-FLAGS"
        status, payload = self._require_imap().uid("STORE", uid, command, f"({flags})")
        if status != STATUS_OK:
            raise ImapError(f"could not update flags on {uid}: {payload}")

    def search_all_uids(self) -> set[str]:
        """Return every UID in the currently selected mailbox."""
        try:
            status, payload = self._require_imap().uid("SEARCH", "ALL")
        except imaplib.IMAP4.error as error:
            raise ImapError(f"search failed: {error}") from error

        if status != STATUS_OK:
            raise ImapError(f"search failed: {payload}")
        if not isinstance(payload, (list, tuple)):
            raise ImapError(
                f"search returned {type(payload).__name__}, not a list: {payload}"
            )

        tokens: list[bytes] = []
        for item in payload:
            if not isinstance(item, bytes):
                raise ImapError(f"search returned a non-bytes item: {item!r}")
            tokens.extend(item.split())

        try:
            uids = {token.decode("ascii") for token in tokens}
        except UnicodeDecodeError as error:
            raise ImapError(f"search returned non-ASCII UIDs: {payload}") from error
        non_numeric = sorted(uid for uid in uids if not uid.isdigit())
        if non_numeric:
            raise ImapError(f"search returned non-numeric UIDs: {non_numeric}")
        return uids

    def move(self, uid: str, destination: str) -> str | None:
        """Move one message and return its destination UID when reported.

        COPYUID is the response code used by most servers; MOVEUID is used by
        some servers implementing RFC 6851.  ``response`` is imaplib's public
        response-code API and must be queried immediately after the command.
        """
        status, payload = self._require_imap().uid(
            "MOVE", uid, _quote_mailbox(destination)
        )
        if status != STATUS_OK:
            raise ImapError(f"could not move {uid} to {destination}: {payload}")
        imap = self._require_imap()
        for code in ("COPYUID", "MOVEUID"):
            _status, response = imap.response(code)
            destination_uid = self._destination_uid(response)
            if destination_uid is not None:
                return destination_uid
        return None

    @staticmethod
    def _destination_uid(response: object) -> str | None:
        """Extract a single destination UID from a COPYUID/MOVEUID response."""
        values = response if isinstance(response, (list, tuple)) else [response]
        text = " ".join(
            value.decode("ascii", "replace") if isinstance(value, bytes) else str(value)
            for value in values
            if value is not None
        )
        match = re.search(r"\b\d+\s+\d+(?::\d+)?\s+(\d+)(?::\d+)?\b", text)
        return match.group(1) if match else None

    def fetch_recent_headers(
        self, exists: int, limit: int, offset: int = 0
    ) -> list[FetchedHeader]:
        """Fetch UID + flags + a few headers for a window of `limit` messages,
        `offset` messages back from the newest. offset=0 is the newest page;
        offset=50 is the 50 before that, and so on (used for load-on-scroll)."""
        if exists == 0:
            return []

        end = exists - offset
        if end < 1:
            return []
        start = max(1, end - limit + 1)  # exists=1000,limit=50,offset=50 -> 901:950
        status, payload = self._require_imap().fetch(
            f"{start}:{end}",
            # BODY.PEEK[...] = look at the header WITHOUT marking it \Seen.
            "(UID FLAGS BODY.PEEK[HEADER.FIELDS "
            "(DATE FROM TO CC SUBJECT MESSAGE-ID IN-REPLY-TO REFERENCES)])",
        )
        if status != STATUS_OK:
            raise ImapError(f"fetch failed: {payload}")

        messages: list[FetchedHeader] = []
        for item in payload:
            # imaplib hands each message back as a tuple of metadata bytes
            # followed by header bytes.  The stray ")" closing lines arrive as
            # plain bytes instead — we skip those.
            if not isinstance(item, tuple):
                continue
            meta, header_bytes = item
            messages.append(self._parse(meta.decode("utf-8", "replace"), header_bytes))
        return messages

    def fetch_message(self, uid: str) -> bytes:
        """Fetch one full message (headers + body) by its stable UID.

        Does not mark it seen.
        """
        status, payload = self._require_imap().uid("fetch", uid, "(BODY.PEEK[])")
        if status != STATUS_OK:
            raise ImapError(f"could not fetch message {uid}: {payload}")

        for item in payload:
            if isinstance(item, tuple):
                return item[1]

        raise ImapError(f"no message body returned for uid {uid}")

    def _parse(self, meta: str, header_bytes: bytes) -> FetchedHeader:
        uid = re.search(r"UID (\d+)", meta)
        flags = re.search(r"FLAGS \(([^)]*)\)", meta)
        flag_text = flags.group(1) if flags else ""

        # Let the stdlib decode the header block: it handles line folding and
        # the =?utf-8?...?= encoding you'd otherwise see as gibberish. Full MIME
        # body parsing with GMime comes in Phase 6 — this is just three headers.
        headers = email.message_from_bytes(header_bytes, policy=policy.default)

        def header(name: str) -> str:
            value = headers[name]
            return str(value) if value else ""

        return FetchedHeader(
            uid=uid.group(1) if uid else "",
            from_header=header("From"),
            to_header=header("To"),
            cc_header=header("Cc"),
            subject=header("Subject"),
            date=header("Date"),
            message_id=header("Message-ID"),
            in_reply_to=header("In-Reply-To"),
            references=header("References"),
            seen=FLAG_SEEN in flag_text,
            flagged=FLAG_FLAGGED in flag_text,
        )
