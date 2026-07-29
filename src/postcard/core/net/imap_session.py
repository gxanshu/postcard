import base64
import email
import imaplib
import re
from email import policy
from typing import NamedTuple


class MailboxInfo(NamedTuple):
    name: str
    delimiter: str  # "" when the server reports NIL: a flat namespace
    flags: str


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
            self._imap = imaplib.IMAP4(self._host, self._port, timeout=30)
            self._imap.starttls()
        else:
            self._imap = imaplib.IMAP4_SSL(self._host, self._port, timeout=30)
        return self._imap.welcome.decode("utf-8", "replace")

    def login(self, user: str, password: str) -> None:
        try:
            if self._imap is not None:
                self._imap.login(user, password)
        except imaplib.IMAP4.error as error:
            raise ImapError(str(error)) from error

    def logout(self) -> None:
        try:
            if self._imap is not None:
                self._imap.logout()
        except Exception:
            pass

    def _require_imap(self) -> imaplib.IMAP4:
        if self._imap is None:
            raise ImapError("not connected")
        return self._imap

    def list_folders(self) -> list[MailboxInfo]:
        """Return every listed mailbox.

        \\Noselect containers are included so the caller can rebuild the
        hierarchy. The delimiter is "" when the server reports NIL, meaning a
        flat namespace whose names must not be split into parent and child.
        """
        typ, data = self._require_imap().list()
        result: list[MailboxInfo] = []
        for raw in data:
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

    def select(self, mailbox: str, readonly: bool = True) -> int:
        """Open a mailbox; return how many messages it holds.

        readonly=True (the default) keeps us non-destructive and never marks
        mail as read. Flag/move actions open it writable.
        """
        typ, data = self._require_imap().select(
            _quote_mailbox(mailbox), readonly=readonly
        )
        if typ != "OK":
            raise ImapError(f"could not open {mailbox}: {data}")
        return int(data[0]) if data and data[0] else 0

    def store_flags(self, uid: str, flags: str, add: bool) -> None:
        """Add or remove flags (e.g. "\\Seen") on one message by UID."""
        command = "+FLAGS" if add else "-FLAGS"
        typ, data = self._require_imap().uid("STORE", uid, command, f"({flags})")
        if typ != "OK":
            raise ImapError(f"could not update flags on {uid}: {data}")

    def search_all_uids(self) -> set[str]:
        """Return every UID in the currently selected mailbox."""
        try:
            typ, data = self._require_imap().uid("SEARCH", "ALL")
        except imaplib.IMAP4.error as error:
            raise ImapError(f"search failed: {error}") from error

        if typ != "OK":
            raise ImapError(f"search failed: {data}")
        if not isinstance(data, (list, tuple)):
            raise ImapError(f"search returned malformed data: {data}")

        tokens: list[bytes] = []
        for item in data:
            if not isinstance(item, bytes):
                raise ImapError(f"search returned malformed data: {data}")
            tokens.extend(item.split())

        try:
            uids = {token.decode("ascii") for token in tokens}
        except UnicodeDecodeError as error:
            raise ImapError(f"search returned malformed data: {data}") from error
        if any(not uid.isdigit() for uid in uids):
            raise ImapError(f"search returned malformed data: {data}")
        return uids

    def move(self, uid: str, destination: str) -> str | None:
        """Move one message and return its destination UID when reported.

        COPYUID is the response code used by most servers; MOVEUID is used by
        some servers implementing RFC 6851.  ``response`` is imaplib's public
        response-code API and must be queried immediately after the command.
        """
        typ, data = self._require_imap().uid("MOVE", uid, _quote_mailbox(destination))
        if typ != "OK":
            raise ImapError(f"could not move {uid} to {destination}: {data}")
        imap = self._require_imap()
        for code in ("COPYUID", "MOVEUID"):
            _typ, response = imap.response(code)
            destination_uid = self._destination_uid(response)
            if destination_uid is not None:
                return destination_uid
        return None

    @staticmethod
    def _destination_uid(response: object) -> str | None:
        """Extract a single destination UID from a COPYUID/MOVEUID response."""
        if isinstance(response, (list, tuple)):
            values = response
        else:
            values = [response]
        text = " ".join(
            value.decode("ascii", "replace") if isinstance(value, bytes) else str(value)
            for value in values
            if value is not None
        )
        match = re.search(r"\b\d+\s+\d+(?::\d+)?\s+(\d+)(?::\d+)?\b", text)
        return match.group(1) if match else None

    def fetch_recent_headers(
        self, exists: int, limit: int, offset: int = 0
    ) -> list[dict]:
        """Fetch UID + flags + a few headers for a window of `limit` messages,
        `offset` messages back from the newest. offset=0 is the newest page;
        offset=50 is the 50 before that, and so on (used for load-on-scroll)."""
        if exists == 0:
            return []

        end = exists - offset
        if end < 1:
            return []
        start = max(1, end - limit + 1)  # exists=1000,limit=50,offset=50 -> 901:950
        typ, data = self._require_imap().fetch(
            f"{start}:{end}",
            # BODY.PEEK[...] = look at the header WITHOUT marking it \Seen.
            "(UID FLAGS BODY.PEEK[HEADER.FIELDS "
            "(DATE FROM TO CC SUBJECT MESSAGE-ID IN-REPLY-TO REFERENCES)])",
        )
        if typ != "OK":
            raise ImapError(f"fetch failed: {data}")

        messages: list[dict] = []
        for item in data:
            # imaplib hands each message back as a tuple:
            #   (metadata_bytes, header_bytes)
            # The stray ")" closing lines arrive as plain bytes — we skip those.
            if not isinstance(item, tuple):
                continue
            meta, header_bytes = item
            messages.append(self._parse(meta.decode("utf-8", "replace"), header_bytes))
        return messages

    def fetch_message(self, uid: str) -> bytes:
        """Fetch one full message (headers + body) by its stable UID.

        Does not mark it seen.
        """
        type, data = self._require_imap().uid("fetch", uid, "(BODY.PEEK[])")
        if type != "OK":
            raise ImapError(f"could not fetch message {uid}: {data}")

        for item in data:
            if isinstance(item, tuple):
                return item[1]

        raise ImapError(f"no message body returned for uid {uid}")

    def _parse(self, meta: str, header_bytes: bytes) -> dict:
        uid = re.search(r"UID (\d+)", meta)
        flags = re.search(r"FLAGS \(([^)]*)\)", meta)
        flag_text = flags.group(1) if flags else ""
        seen = "\\Seen" in flag_text
        flagged = "\\Flagged" in flag_text

        # Let the stdlib decode the header block: it handles line folding and
        # the =?utf-8?...?= encoding you'd otherwise see as gibberish. Full MIME
        # body parsing with GMime comes in Phase 6 — this is just three headers.
        headers = email.message_from_bytes(header_bytes, policy=policy.default)
        return {
            "uid": uid.group(1) if uid else "",
            "from": str(headers["From"]) if headers["From"] else "",
            "to": str(headers["To"]) if headers["To"] else "",
            "cc": str(headers["Cc"]) if headers["Cc"] else "",
            "subject": str(headers["Subject"]) if headers["Subject"] else "",
            "date": str(headers["Date"]) if headers["Date"] else "",
            "message_id": str(headers["Message-ID"]) if headers["Message-ID"] else "",
            "in_reply_to": str(headers["In-Reply-To"])
            if headers["In-Reply-To"]
            else "",
            "references": str(headers["References"]) if headers["References"] else "",
            "seen": seen,
            "flagged": flagged,
        }
