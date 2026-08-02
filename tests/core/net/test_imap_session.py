import pytest

from postcard.core.net.imap_session import FetchedHeader, ImapError, ImapSession


class FakeImap:
    def __init__(self, search_reply=("OK", [b"4 9 20"]), fetch_reply=None):
        self.calls = []
        self.welcome = b"fake imap"
        self._search_reply = search_reply
        self._fetch_reply = fetch_reply

    def uid(self, *args):
        self.calls.append(args)
        return self._search_reply

    def fetch(self, *args):
        self.calls.append(args)
        return self._fetch_reply


def connect(monkeypatch, imap: FakeImap) -> ImapSession:
    monkeypatch.setattr(
        "postcard.core.net.imap_session.imaplib.IMAP4_SSL",
        lambda *args, **kwargs: imap,
    )
    session = ImapSession("imap.example.com", 993)
    session.connect()
    return session


def test_search_all_uids_uses_uid_search_all_and_returns_the_uids(monkeypatch):
    imap = FakeImap()
    session = connect(monkeypatch, imap)

    assert session.search_all_uids() == {"4", "9", "20"}
    assert imap.calls == [("SEARCH", "ALL")]


# --- search failure messages ------------------------------------------------
# Each of these used to raise the same "malformed data" text, which said
# nothing about which of the four checks had tripped.


def test_a_non_ok_status_says_the_search_failed(monkeypatch):
    session = connect(monkeypatch, FakeImap(search_reply=("NO", [b"nope"])))
    with pytest.raises(ImapError, match="search failed"):
        session.search_all_uids()


def test_a_non_list_payload_names_the_type_it_got(monkeypatch):
    session = connect(monkeypatch, FakeImap(search_reply=("OK", "4 9")))
    with pytest.raises(ImapError, match="search returned str, not a list"):
        session.search_all_uids()


def test_a_non_bytes_item_is_reported_as_such(monkeypatch):
    session = connect(monkeypatch, FakeImap(search_reply=("OK", [b"4", 9])))
    with pytest.raises(ImapError, match="non-bytes item"):
        session.search_all_uids()


def test_non_numeric_uids_are_listed(monkeypatch):
    session = connect(monkeypatch, FakeImap(search_reply=("OK", [b"4 bogus"])))
    with pytest.raises(ImapError, match=r"non-numeric UIDs: \['bogus'\]"):
        session.search_all_uids()


# --- fetch_recent_headers ---------------------------------------------------

HEADER_BYTES = (
    b"Date: Thu, 16 Jul 2026 10:00:00 +0000\r\n"
    b"From: Ada Lovelace <ada@example.com>\r\n"
    b"To: Me <me@example.com>\r\n"
    b"Subject: Lunch\r\n"
    b"Message-ID: <a@example.com>\r\n"
    b"\r\n"
)


def test_fetch_recent_headers_returns_typed_rows(monkeypatch):
    # Arrange: one message, seen but not flagged, plus the stray ")" line
    # imaplib appends as plain bytes rather than a tuple.
    reply = (
        "OK",
        [(b"1 (UID 42 FLAGS (\\Seen) BODY[HEADER.FIELDS ...]", HEADER_BYTES), b")"],
    )
    session = connect(monkeypatch, FakeImap(fetch_reply=reply))

    # Act
    (header,) = session.fetch_recent_headers(exists=1, limit=50)

    # Assert: raw wire values, not display-ready ones -- the date is still the
    # original RFC 5322 string and the address is unparsed.
    assert isinstance(header, FetchedHeader)
    assert header.uid == "42"
    assert header.from_header == "Ada Lovelace <ada@example.com>"
    assert header.subject == "Lunch"
    assert header.date == "Thu, 16 Jul 2026 10:00:00 +0000"
    assert header.seen is True
    assert header.flagged is False


def test_a_missing_header_reads_back_as_empty(monkeypatch):
    reply = ("OK", [(b"1 (UID 7 FLAGS ()", b"Subject: Hi\r\n\r\n")])
    session = connect(monkeypatch, FakeImap(fetch_reply=reply))

    (header,) = session.fetch_recent_headers(exists=1, limit=50)

    assert header.cc_header == ""
    assert header.in_reply_to == ""
    assert header.references == ""
    assert header.seen is False


def test_an_empty_mailbox_is_not_fetched_at_all(monkeypatch):
    imap = FakeImap()
    session = connect(monkeypatch, imap)

    assert session.fetch_recent_headers(exists=0, limit=50) == []
    assert imap.calls == []


def test_paging_past_the_oldest_message_returns_nothing(monkeypatch):
    imap = FakeImap()
    session = connect(monkeypatch, imap)

    assert session.fetch_recent_headers(exists=10, limit=50, offset=10) == []
    assert imap.calls == []


# --- append -----------------------------------------------------------------


class AppendingImap(FakeImap):
    def __init__(self, reply=("OK", [b"[APPENDUID 1 7] APPEND completed"])):
        super().__init__()
        self._reply = reply
        self.capabilities = ("IMAP4REV1", "X-GM-EXT-1")

    def append(self, *args):
        self.calls.append(args)
        return self._reply


def test_append_quotes_the_mailbox_and_stores_the_copy_as_read(monkeypatch):
    # A Sent mailbox with a space in its name arrives as two tokens unless it
    # is quoted, and the server answers BAD.
    imap = AppendingImap()
    session = connect(monkeypatch, imap)

    session.append("[Gmail]/Sent Mail", b"raw")

    assert imap.calls == [('"[Gmail]/Sent Mail"', "\\Seen", None, b"raw")]


def test_append_raises_when_the_server_refuses(monkeypatch):
    session = connect(monkeypatch, AppendingImap(reply=("NO", [b"[TRYCREATE]"])))

    with pytest.raises(ImapError, match="could not append to Sent"):
        session.append("Sent", b"raw")


def test_has_capability_is_case_insensitive(monkeypatch):
    session = connect(monkeypatch, AppendingImap())

    assert session.has_capability("x-gm-ext-1")
    assert not session.has_capability("X-SOMETHING-ELSE")
