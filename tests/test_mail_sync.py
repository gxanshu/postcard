import pytest

import postcard.mail_sync as mail_sync
from postcard.core.models.account import Account
from postcard.core.models.conversation import Conversation
from postcard.core.models.email import Email
from postcard.core.net.imap_session import (
    GMAIL_CAPABILITY,
    FetchedHeader,
    ImapError,
    MailboxInfo,
)
from postcard.mail_sync import (
    NO_SUBJECT,
    FolderRole,
    SyncResult,
    _to_message_header,
    display_name_for_folder,
    fetch_mailbox,
    icon_for_folder,
    inbox_name,
    parent_mailbox_name,
    role_for_folder,
    server_uids,
)


def conversation(*server_ids: str | None) -> Conversation:
    return Conversation(
        [
            Email(
                id=i,
                folder_id=1,
                server_id=uid,
                sender="a@x",
                subject="Lunch",
                preview="",
                date="",
                is_unread=False,
            )
            for i, uid in enumerate(server_ids, start=1)
        ]
    )


@pytest.mark.parametrize(
    ("name", "role"),
    [
        ("INBOX", "inbox"),
        ("inbox", "inbox"),
        ("[Gmail]/Sent Mail", "sent"),
        ("Sent Items", "sent"),
        ("INBOX.Drafts", "drafts"),
        ("Trash", "trash"),
        ("Deleted Items", "trash"),
        ("Junk E-mail", "junk"),
        ("Spam", "junk"),
        ("Archive", "archive"),
        ("[Gmail]/All Mail", "archive"),
        ("Flagged", "starred"),
        ("Notes", "other"),
        ("", "other"),
    ],
)
def test_role_for_folder(name, role):
    assert role_for_folder(name) == role


def test_only_a_folder_named_exactly_inbox_is_the_inbox():
    assert role_for_folder("inbox/receipts") == "other"


def test_the_first_matching_role_wins():
    # Substrings are checked in order, so "sent" beats the later "draft".
    assert role_for_folder("Sent/Drafts") == "sent"


def test_role_for_folder_returns_a_folder_role_member():
    assert role_for_folder("INBOX") is FolderRole.INBOX
    assert role_for_folder("Notes") is FolderRole.OTHER


def test_folder_role_members_keep_their_stored_string_values():
    # The role is persisted and compared as a plain lowercase string, so a
    # StrEnum member has to stay interchangeable with the literal.
    assert FolderRole.ARCHIVE == "archive"
    assert f"{FolderRole.TRASH}" == "trash"
    assert {"inbox": 1}[FolderRole.INBOX] == 1
    # No member carries a Python naming prefix: these are stored strings, and
    # an is_/has_ rename sweeping over them would only surface at runtime.
    assert not [role for role in FolderRole if role.startswith(("is_", "has_"))]


def test_icon_for_folder():
    assert icon_for_folder("INBOX") == "mail-unread-symbolic"
    assert icon_for_folder("[Gmail]/Sent Mail") == "mail-send-symbolic"
    assert icon_for_folder("Drafts") == "document-edit-symbolic"
    assert icon_for_folder("Trash") == "user-trash-symbolic"
    assert icon_for_folder("Spam") == "mail-mark-junk-symbolic"
    assert icon_for_folder("Flagged") == "starred-symbolic"
    assert icon_for_folder("Notes") == "folder-symbolic"


def test_archive_falls_back_to_the_generic_folder_icon():
    # Deliberate: Adwaita ships no mail-archive-symbolic, and a missing icon
    # renders as a broken image.
    assert icon_for_folder("Archive") == "folder-symbolic"
    assert icon_for_folder("[Gmail]/All Mail") == "folder-symbolic"


def test_display_name_strips_the_gmail_namespace():
    assert display_name_for_folder("[Gmail]/Sent Mail") == "Sent Mail"
    assert display_name_for_folder("[Google Mail]/Trash", "/") == "Trash"


def test_display_name_keeps_a_namespace_root_that_stands_alone():
    assert display_name_for_folder("[Gmail]") == "[Gmail]"


def test_display_name_shows_only_the_last_path_segment():
    assert display_name_for_folder("Work/Clients/Acme", "/") == "Acme"
    assert display_name_for_folder("INBOX.Receipts", ".") == "Receipts"


def test_display_name_without_a_delimiter_keeps_the_full_path():
    assert display_name_for_folder("Work/Clients/Acme") == "Work/Clients/Acme"


def test_display_name_decodes_modified_utf7():
    assert display_name_for_folder("INBOX.Entw&APw-rfe", ".") == "Entwürfe"
    assert display_name_for_folder("A&-B") == "A&B"


def test_inbox_name_prefers_the_server_s_own_spelling():
    assert inbox_name(["Sent", "Inbox", "Trash"]) == "Inbox"


def test_inbox_name_falls_back_when_the_server_lists_none():
    assert inbox_name([]) == "INBOX"
    assert inbox_name(["Sent"]) == "INBOX"


def test_parent_mailbox_name():
    assert parent_mailbox_name("Work/Clients/Acme", "/") == "Work/Clients"
    assert parent_mailbox_name("INBOX.Receipts", ".") == "INBOX"


def test_a_top_level_mailbox_has_no_parent():
    assert parent_mailbox_name("Archive", "/") == ""
    assert parent_mailbox_name("Archive", "") == ""


def test_a_namespace_root_does_not_count_as_a_parent():
    assert parent_mailbox_name("[Gmail]/Sent Mail", "/") == ""


def test_server_uids_collects_every_uid():
    assert server_uids(conversation("4", "9")) == ["4", "9"]


def test_server_uids_skips_messages_the_server_has_never_seen():
    # imaplib silently drops a None argument, which would send a UID-less
    # "UID STORE +FLAGS (...)" and come back BAD.
    assert server_uids(conversation("4", None, "9")) == ["4", "9"]
    assert server_uids(conversation(None)) == []


@pytest.mark.parametrize(
    ("offset", "snapshot", "search_calls"),
    [
        (0, {"4", "9"}, 1),
        (0, set(), 1),
        (50, None, 0),
    ],
)
def test_fetch_mailbox_returns_an_authoritative_uid_snapshot_only_for_newest_page(
    monkeypatch, offset, snapshot, search_calls
):
    class FakeImapSession:
        searches = 0

        def __init__(self, host, port, security):
            pass

        def connect(self):
            pass

        def login(self, user, password):
            pass

        def list_folders(self):
            return []

        def select(self, mailbox):
            return 0

        def search_all_uids(self):
            type(self).searches += 1
            return snapshot

        def fetch_recent_headers(self, exists, limit, offset):
            return []

        def logout(self):
            pass

    monkeypatch.setattr(mail_sync, "ImapSession", FakeImapSession)
    account = Account(
        id=1,
        email="ada@example.com",
        display_name="Ada",
        imap_host="imap.example.com",
        imap_port=993,
        smtp_host="",
        smtp_port=0,
    )

    result = fetch_mailbox(account, "password", offset=offset)

    assert FakeImapSession.searches == search_calls
    assert result.all_uids == snapshot
    assert SyncResult().all_uids is None


# --- unread counts for the folders this sync did not fetch ------------------


class CountingImapSession:
    """An IMAP server that records which mailboxes were asked for a count."""

    mailboxes: list[MailboxInfo] = []
    refused: str = ""
    asked: list[str] = []

    def __init__(self, host, port, security):
        pass

    def connect(self):
        pass

    def login(self, user, password):
        pass

    def list_folders(self):
        return type(self).mailboxes

    def select(self, mailbox):
        return 0

    def search_all_uids(self):
        return set()

    def fetch_recent_headers(self, exists, limit, offset):
        return []

    def unseen_count(self, mailbox):
        type(self).asked.append(mailbox)
        if mailbox == type(self).refused:
            raise ImapError("mailbox unavailable")
        return 7

    def logout(self):
        pass


@pytest.fixture
def counting_imap(monkeypatch):
    CountingImapSession.asked = []
    CountingImapSession.refused = ""
    CountingImapSession.mailboxes = [
        MailboxInfo("INBOX", "/", ""),
        MailboxInfo("Junk", "/", ""),
        MailboxInfo("[Gmail]", "/", "\\Noselect"),
        MailboxInfo("Receipts", "/", ""),
    ]
    monkeypatch.setattr(mail_sync, "ImapSession", CountingImapSession)
    return CountingImapSession


def sync(offset: int = 0) -> SyncResult:
    account = Account(
        id=1,
        email="ada@example.com",
        display_name="Ada",
        imap_host="imap.example.com",
        imap_port=993,
        smtp_host="",
        smtp_port=0,
    )
    return mail_sync.fetch_mailbox(account, "hunter2", offset=offset)


def test_every_role_folder_but_the_fetched_one_is_counted(counting_imap):
    # Only the selected mailbox is fetched, so nothing else would ever refresh.
    assert sync().unread_counts == {"Junk": 7}


def test_containers_and_folders_without_a_role_are_not_counted(counting_imap):
    # "[Gmail]" cannot hold mail; "Receipts" is an ordinary folder, and one
    # STATUS per user folder would make every sync as slow as the folder list.
    sync()

    assert counting_imap.asked == ["Junk"]


def test_a_folder_that_refuses_a_count_does_not_fail_the_sync(counting_imap):
    counting_imap.refused = "Junk"
    counting_imap.mailboxes.append(MailboxInfo("Archive", "/", ""))

    assert sync().unread_counts == {"Archive": 7}


def test_paging_older_mail_does_not_re_poll_the_counts(counting_imap):
    result = sync(offset=50)

    assert result.unread_counts == {}
    assert counting_imap.asked == []


# --- raw wire headers -> display-ready MessageHeader ------------------------


def fetched(**overrides) -> FetchedHeader:
    fields = {
        "uid": "42",
        "from_header": "Ada Lovelace <ada@example.com>",
        "to_header": "Me <me@example.com>",
        "cc_header": "",
        "subject": "Lunch",
        "date": "Thu, 16 Jul 2026 10:00:00 +0000",
        "message_id": "<a@example.com>",
        "in_reply_to": "",
        "references": "",
        "seen": False,
        "flagged": False,
    }
    return FetchedHeader(**{**fields, **overrides})


def test_the_display_name_and_address_are_split_out():
    header = _to_message_header(fetched())
    assert header.sender == "Ada Lovelace"
    assert header.sender_address == "ada@example.com"


def test_a_bare_address_becomes_its_own_display_name():
    header = _to_message_header(fetched(from_header="ada@example.com"))
    assert header.sender == "ada@example.com"
    assert header.sender_address == "ada@example.com"


def test_the_date_is_shortened_for_the_list():
    assert _to_message_header(fetched()).date == "Jul 16"


def test_an_unparseable_date_is_passed_through_unchanged():
    assert _to_message_header(fetched(date="whenever")).date == "whenever"


def test_the_seen_flag_is_inverted_into_unread():
    # The single most confusable field in the mapping: the server reports what
    # has been read, the app tracks what has not.
    assert _to_message_header(fetched(seen=False)).is_unread is True
    assert _to_message_header(fetched(seen=True)).is_unread is False


def test_the_flagged_flag_becomes_starred():
    assert _to_message_header(fetched(flagged=True)).is_starred is True


def test_a_missing_subject_gets_the_placeholder():
    # NO_SUBJECT, never a translated string -- see the threader tests.
    assert _to_message_header(fetched(subject="")).subject == NO_SUBJECT


def test_every_address_on_the_message_is_collected_for_contacts():
    header = _to_message_header(
        fetched(
            from_header="Ada <ada@example.com>",
            to_header="Me <me@example.com>",
            cc_header="Grace <grace@example.com>",
        )
    )
    assert header.addresses == [
        ("Ada", "ada@example.com"),
        ("Me", "me@example.com"),
        ("Grace", "grace@example.com"),
    ]


def test_the_uid_and_threading_headers_carry_over_verbatim():
    header = _to_message_header(
        fetched(uid="7", message_id="<b>", in_reply_to="<a>", references="<a> <x>")
    )
    assert (header.uid, header.message_id) == ("7", "<b>")
    assert (header.in_reply_to, header.references) == ("<a>", "<a> <x>")


# --- filing a copy of a sent message in Sent --------------------------------


class FakeSmtpSession:
    def __init__(self, host, port, security):
        pass

    def connect(self):
        pass

    def login(self, user, password):
        pass

    def send_raw(self, from_addr, recipients, raw):
        pass

    def quit(self):
        pass


class AppendingImapSession:
    """An IMAP server that records what was appended where."""

    appends: list[tuple[str, bytes]] = []
    mailboxes: list[str] = []
    capabilities: tuple[str, ...] = ()

    def __init__(self, host, port, security):
        pass

    def connect(self):
        pass

    def login(self, user, password):
        pass

    def has_capability(self, name):
        return name in type(self).capabilities

    def list_folders(self):
        return [MailboxInfo(name, "/", "") for name in type(self).mailboxes]

    def append(self, mailbox, raw):
        type(self).appends.append((mailbox, raw))

    def logout(self):
        pass


@pytest.fixture
def imap(monkeypatch):
    AppendingImapSession.appends = []
    AppendingImapSession.mailboxes = ["INBOX", "[Gmail]/Sent Mail"]
    AppendingImapSession.capabilities = ()
    monkeypatch.setattr(mail_sync, "SmtpSession", FakeSmtpSession)
    monkeypatch.setattr(mail_sync, "ImapSession", AppendingImapSession)
    return AppendingImapSession


def send() -> None:
    account = Account(
        id=1,
        email="ada@example.com",
        display_name="Ada",
        imap_host="imap.example.com",
        imap_port=993,
        smtp_host="smtp.example.com",
        smtp_port=465,
    )
    mail_sync.send_message(
        account, "hunter2", "ada@example.com", ["you@example.com"], b"raw"
    )


def test_a_sent_message_is_appended_to_the_server_s_sent_mailbox(imap):
    # SMTP alone only delivers to the recipient -- every other client would
    # show an empty Sent folder.
    send()

    assert imap.appends == [("[Gmail]/Sent Mail", b"raw")]


def test_gmail_files_its_own_copy_so_nothing_is_appended(imap):
    imap.capabilities = (GMAIL_CAPABILITY,)

    send()

    assert imap.appends == []


def test_a_server_without_a_sent_mailbox_is_skipped_rather_than_failing(imap):
    imap.mailboxes = ["INBOX"]

    send()

    assert imap.appends == []


def test_the_send_still_counts_as_done_when_the_append_fails(imap, monkeypatch):
    # The mail has already left over SMTP. Raising here would report a failure
    # and leave it in the Outbox, which sends it a second time.
    def refuse(self, mailbox, raw):
        raise ImapError("over quota")

    monkeypatch.setattr(imap, "append", refuse)

    send()
