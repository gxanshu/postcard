import pytest

import postcard.mail_sync as mail_sync
from postcard.core.models.account import Account
from postcard.core.models.conversation import Conversation
from postcard.core.models.email import Email
from postcard.mail_sync import (
    FolderRole,
    SyncResult,
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
                unread=False,
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
