from postcard.core.net.imap_session import ImapSession


class FakeImap:
    def __init__(self):
        self.calls = []
        self.welcome = b"fake imap"

    def uid(self, *args):
        self.calls.append(args)
        return "OK", [b"4 9 20"]


def test_search_all_uids_uses_uid_search_all_and_returns_the_uids(monkeypatch):
    imap = FakeImap()
    monkeypatch.setattr(
        "postcard.core.net.imap_session.imaplib.IMAP4_SSL",
        lambda *args, **kwargs: imap,
    )
    session = ImapSession("imap.example.com", 993)
    session.connect()

    assert session.search_all_uids() == {"4", "9", "20"}
    assert imap.calls == [("SEARCH", "ALL")]
