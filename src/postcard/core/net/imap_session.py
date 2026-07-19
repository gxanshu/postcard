import email
import imaplib
import re
from email import policy


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

    def _require_imap(self) -> imaplib.IMAP4_SSL:
        if self._imap is None:
            raise ImapError("not connected")
        return self._imap

    def list_folders(self) -> list[str]:
        """Return the names of the account's selectable mailboxes."""
        typ, data = self._require_imap().list()
        names: list[str] = []
        for raw in data:
            # raw is bytes like:  (\HasNoChildren) "/" "INBOX"
            if not isinstance(raw, bytes):
                continue
            line = raw.decode("utf-8", "replace")
            match = re.match(r'\(([^)]*)\) (?:"[^"]*"|NIL) (.+)$', line)
            if match is None:
                continue
            flags, name = match.group(1), match.group(2).strip()
            if "\\Noselect" in flags:
                continue  # a container like "[Gmail]" you can't actually open
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1]  # strip the surrounding quotes
            names.append(name)
        return names

    def select(self, mailbox: str, readonly: bool = True) -> int:
        """Open a mailbox; return how many messages it holds.

        readonly=True (the default) keeps us non-destructive and never marks
        mail as read. Flag/move actions open it writable.
        """
        typ, data = self._require_imap().select(mailbox, readonly=readonly)
        if typ != "OK":
            raise ImapError(f"could not open {mailbox}: {data}")
        return int(data[0]) if data and data[0] else 0

    def store_flags(self, uid: str, flags: str, add: bool) -> None:
        """Add or remove flags (e.g. "\\Seen") on one message by UID."""
        command = "+FLAGS" if add else "-FLAGS"
        typ, data = self._require_imap().uid("STORE", uid, command, f"({flags})")
        if typ != "OK":
            raise ImapError(f"could not update flags on {uid}: {data}")

    def move(self, uid: str, destination: str) -> None:
        """Move one message by UID to another mailbox (RFC 6851 MOVE)."""
        typ, data = self._require_imap().uid("MOVE", uid, destination)
        if typ != "OK":
            raise ImapError(f"could not move {uid} to {destination}: {data}")

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
            "(DATE FROM SUBJECT MESSAGE-ID IN-REPLY-TO REFERENCES)])",
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
