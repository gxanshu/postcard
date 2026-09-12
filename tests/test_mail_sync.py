from datetime import date, datetime, timedelta

import pytest

import postcard.mail_sync as mail_sync
from postcard.core.models.account import Account
from postcard.core.models.conversation import Conversation
from postcard.core.models.email import Email
from postcard.core.models.folder import Folder
from postcard.core.net.auth import Credential
from postcard.core.net.graph_folders import GraphFolder
from postcard.core.net.graph_messages import DeltaState, MoveOutcome
from postcard.core.net.imap_session import (
    FLAG_SEEN,
    GMAIL_CAPABILITY,
    FetchedHeader,
    ImapError,
    MailboxInfo,
)
from postcard.core.store.database import Database
from postcard.mail_sync import (
    NO_SUBJECT,
    FolderRole,
    SyncResult,
    _to_message_header,
    creation_order,
    display_name_for_folder,
    drafts_folder,
    fetch_full_message,
    fetch_mailbox,
    first_recipient,
    folder_label,
    folder_role,
    format_date,
    icon_for_folder,
    icon_for_mailbox,
    inbox_name,
    is_outgoing,
    is_outgoing_folder,
    mailbox_with_role,
    move_messages,
    parent_mailbox_name,
    parent_of,
    role_for_folder,
    send_message,
    sent_folder,
    server_uids,
)

CREDENTIAL = Credential("ada@example.com", "hunter2")


def account(smtp_host: str = "", smtp_port: int = 0) -> Account:
    """The account handed to mail_sync. The SMTP half is only filled in for the
    tests that send; the rest never reach it."""
    return Account(
        id=1,
        email="ada@example.com",
        display_name="Ada",
        imap_host="imap.example.com",
        imap_port=993,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
    )


@pytest.fixture(autouse=True)
def empty_pool():
    # Every test signs in as account id 1, the pool's key, so the first double
    # pooled would otherwise answer every later test's commands.
    mail_sync.close_sessions()
    yield
    mail_sync.close_sessions()


@pytest.fixture
def db_account():
    database = Database(":memory:")
    account = database.save_account(
        "me@example.com", "Me", "imap.example.com", 993, "smtp.example.com", 587
    )
    yield database, account.id
    database.close()


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
        ("Sent Messages", "sent"),
        ("INBOX.Sent", "sent"),
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
    # Patterns are tried in order, so "sent" beats the later "draft".
    assert role_for_folder("Sent/Drafts") == "sent"


@pytest.mark.parametrize("name", ["Consent forms", "Presentations", "Unsent Messages"])
def test_a_role_word_inside_another_word_is_not_that_role(name):
    # A user folder whose name merely contains "sent" must not be taken for the
    # sent mailbox -- mail sent from Postcard would be filed into it.
    assert role_for_folder(name) is not FolderRole.SENT


def test_mailbox_with_role_finds_the_provider_s_own_spelling():
    assert (
        mailbox_with_role(["INBOX", "Sent Items", "Trash"], FolderRole.SENT)
        == "Sent Items"
    )
    assert (
        mailbox_with_role(["INBOX", "[Gmail]/Sent Mail"], FolderRole.SENT)
        == "[Gmail]/Sent Mail"
    )


def test_mailbox_with_role_returns_none_when_the_server_lists_none():
    assert mailbox_with_role(["INBOX", "Notes"], FolderRole.SENT) is None


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


def test_archive_uses_the_bundled_archive_icon():
    # Adwaita ships no mail-archive-symbolic, so we bundle one in the
    # GResource -- the same icon the reader's Archive button uses.
    assert icon_for_folder("Archive") == "mail-archive-symbolic"
    assert icon_for_folder("[Gmail]/All Mail") == "mail-archive-symbolic"


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


def test_sent_folder_uses_the_synced_mailbox_rather_than_a_new_one(db_account):
    db, account_id = db_account
    db.get_or_create_folder(account_id, "INBOX")
    db.get_or_create_folder(account_id, "[Gmail]/Sent Mail")

    folder = sent_folder(db, account_id)

    assert folder.name == "[Gmail]/Sent Mail"
    assert [f.name for f in db.folders_for_account(account_id)] == [
        "INBOX",
        "[Gmail]/Sent Mail",
    ]


def test_sent_folder_creates_one_before_the_first_sync(db_account):
    db, account_id = db_account

    folder = sent_folder(db, account_id)

    assert folder.name == "Sent"
    assert sent_folder(db, account_id).id == folder.id


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
    assert server_uids(conversation("4", "9").emails) == ["4", "9"]


def test_server_uids_skips_messages_the_server_has_never_seen():
    # imaplib silently drops a None argument, which would send a UID-less
    # "UID STORE +FLAGS (...)" and come back BAD.
    assert server_uids(conversation("4", None, "9").emails) == ["4", "9"]
    assert server_uids(conversation(None).emails) == []


class FakeImapSession:
    """An IMAP server that answers every command with nothing.

    The doubles below subclass this and override only the one command they
    record, so each says what it is for and nothing else. State is per-class
    rather than per-instance because mail_sync constructs the session itself --
    a test only gets to hand it the class, so the fixture resets the class.
    """

    mailboxes: list[MailboxInfo] = []

    def __init__(self, host, port, security):
        pass

    def connect(self):
        pass

    def sign_in(self, credential):
        pass

    def is_alive(self):
        return True

    def list_folders(self):
        return type(self).mailboxes

    def select(self, mailbox, is_readonly=True):
        return 0

    def search_all_uids(self):
        return set()

    def fetch_recent_headers(self, exists, limit, offset):
        return []

    def logout(self):
        pass


def mailboxes(*names: str) -> list[MailboxInfo]:
    """Selectable mailboxes under the usual "/" delimiter."""
    return [MailboxInfo(name, "/", "") for name in names]


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
    # Declared here rather than beside the others: each parametrization needs
    # its own `snapshot` and a counter starting at zero.
    class SearchingImapSession(FakeImapSession):
        searches = 0

        def search_all_uids(self):
            type(self).searches += 1
            return snapshot

    monkeypatch.setattr(mail_sync, "ImapSession", SearchingImapSession)

    result = fetch_mailbox(account(), CREDENTIAL, offset=offset)

    assert SearchingImapSession.searches == search_calls
    assert result.all_uids == snapshot
    assert SyncResult().all_uids is None


class CountingImapSession(FakeImapSession):
    """An IMAP server that records which mailboxes were asked for a count."""

    refused: str = ""
    asked: list[str] = []

    def unseen_count(self, mailbox):
        type(self).asked.append(mailbox)
        if mailbox == type(self).refused:
            raise ImapError("mailbox unavailable")
        return 7


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
    return mail_sync.fetch_mailbox(account(), CREDENTIAL, offset=offset)


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


def test_a_sync_that_opts_out_does_not_poll_the_counts(counting_imap):
    # A folder click syncs the folder it opens; the badges of the ones it
    # doesn't are a STATUS round trip each, and the switch shouldn't wait.
    result = mail_sync.fetch_mailbox(account(), CREDENTIAL, should_count_unread=False)

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


def test_the_first_recipient_is_kept_for_outgoing_folders():
    header = _to_message_header(
        fetched(to_header="Bob <BOB@example.com>, Cleo <cleo@example.com>")
    )
    assert header.recipient == "Bob"
    assert header.recipient_address == "bob@example.com"


@pytest.mark.parametrize("to_header", ["", "undisclosed-recipients:;"])
def test_a_message_addressed_to_nobody_has_no_recipient(to_header):
    header = _to_message_header(fetched(to_header=to_header))
    assert (header.recipient, header.recipient_address) == ("", "")


def test_a_bare_recipient_address_becomes_its_own_display_name():
    assert first_recipient("bob@example.com") == ("bob@example.com", "bob@example.com")


@pytest.mark.parametrize(
    ("name", "is_outgoing"),
    [
        ("Sent", True),
        ("[Gmail]/Sent Mail", True),
        ("Drafts", True),
        ("Outbox", True),
        ("INBOX", False),
        ("Archive", False),
    ],
)
def test_is_outgoing_folder(name, is_outgoing):
    assert is_outgoing_folder(name) is is_outgoing


def test_the_date_is_stored_as_a_timestamp():
    assert _to_message_header(fetched()).date == "2026-07-16T10:00:00+00:00"


def test_an_unparseable_date_is_passed_through_unchanged():
    assert _to_message_header(fetched(date="whenever")).date == "whenever"


def days_ago(days: int, hour: int = 10) -> str:
    """An ISO timestamp `days` before today, on the hour so the label is fixed."""
    now = datetime.now().astimezone()
    on_the_hour = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return (on_the_hour - timedelta(days=days)).isoformat()


def test_todays_mail_shows_the_time():
    assert format_date(days_ago(0, hour=9)) == "Today 09:00"


def test_yesterdays_mail_is_named_not_timed():
    assert format_date(days_ago(1)) == "Yesterday"


def test_mail_from_the_last_week_shows_the_weekday():
    for days in range(2, 7):
        expected = (date.today() - timedelta(days=days)).strftime("%a")
        assert format_date(days_ago(days)) == expected


def test_older_mail_falls_back_to_the_month_and_day():
    seven_days_back = date.today() - timedelta(days=7)
    assert format_date(days_ago(7)) == seven_days_back.strftime("%b %d")
    assert format_date("2026-07-16T10:00:00+00:00") == "Jul 16"


def test_a_future_date_is_not_mistaken_for_a_weekday():
    # A skewed clock on the sending machine, or mail that arrives postdated.
    tomorrow = date.today() + timedelta(days=1)
    assert format_date(days_ago(-1)) == tomorrow.strftime("%b %d")


def test_a_naive_timestamp_is_read_as_local_time():
    naive = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
    assert format_date(naive.isoformat()) == "Today 08:00"


def test_a_date_that_is_not_a_timestamp_is_shown_as_it_is():
    # Rows written before this column held a timestamp, and unreadable headers.
    assert format_date("Jul 16") == "Jul 16"
    assert format_date("") == ""


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
    """An SMTP server that accepts everything and remembers nothing."""

    def __init__(self, host, port, security):
        pass

    def connect(self):
        pass

    def sign_in(self, credential):
        pass

    def send_raw(self, from_addr, recipients, raw):
        pass

    def quit(self):
        pass


class AppendingImapSession(FakeImapSession):
    """An IMAP server that records what was appended where."""

    appends: list[tuple[str, bytes]] = []
    capabilities: tuple[str, ...] = ()

    def has_capability(self, name):
        return name in type(self).capabilities

    def append(self, mailbox, raw):
        type(self).appends.append((mailbox, raw))


@pytest.fixture
def imap(monkeypatch):
    AppendingImapSession.appends = []
    AppendingImapSession.mailboxes = mailboxes("INBOX", "[Gmail]/Sent Mail")
    AppendingImapSession.capabilities = ()
    monkeypatch.setattr(mail_sync, "SmtpSession", FakeSmtpSession)
    monkeypatch.setattr(mail_sync, "ImapSession", AppendingImapSession)
    return AppendingImapSession


def send() -> None:
    mail_sync.send_message(
        account("smtp.example.com", 465),
        CREDENTIAL,
        "ada@example.com",
        ["you@example.com"],
        b"raw",
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
    imap.mailboxes = mailboxes("INBOX")

    send()

    assert imap.appends == []


def test_the_send_still_counts_as_done_when_the_append_fails(imap, monkeypatch):
    # The mail has already left over SMTP. Raising here would report a failure
    # and leave it in the Outbox, which sends it a second time.
    def refuse(self, mailbox, raw):
        raise ImapError("over quota")

    monkeypatch.setattr(imap, "append", refuse)

    send()


# --- flags ------------------------------------------------------------------


class StoringImapSession(FakeImapSession):
    """An IMAP server that records every STORE it was asked to run."""

    stores: list[tuple[str, str, bool]] = []

    def store_flags(self, uids, flags, should_add):
        type(self).stores.append((uids, flags, should_add))


@pytest.fixture
def storing_imap(monkeypatch):
    StoringImapSession.stores = []
    monkeypatch.setattr(mail_sync, "ImapSession", StoringImapSession)
    return StoringImapSession


def set_flag(*uids: str) -> None:
    mail_sync.set_flag(account(), CREDENTIAL, "INBOX", uids, FLAG_SEEN, should_add=True)


def test_a_whole_thread_is_flagged_in_one_store(storing_imap):
    # One STORE per message made reading a long thread a round trip per message.
    set_flag("4", "9", "20")

    assert storing_imap.stores == [("4,9,20", FLAG_SEEN, True)]


def test_flagging_nothing_sends_no_command(storing_imap):
    # A conversation of locally saved copies has no UID yet, and an empty UID
    # set is a malformed command rather than an empty one.
    set_flag()

    assert storing_imap.stores == []


# --- connection pool --------------------------------------------------------


class CountingSession(FakeImapSession):
    """An IMAP server that records how often it was connected to and left."""

    connects = 0
    logouts = 0
    alive = True
    sign_in_fails = False
    select_fails = False
    move_fails = False

    def connect(self):
        type(self).connects += 1

    def sign_in(self, credential):
        if type(self).sign_in_fails:
            raise ImapError("authentication failed")

    def is_alive(self):
        return type(self).alive

    def logout(self):
        type(self).logouts += 1

    def select(self, mailbox, is_readonly=True):
        if type(self).select_fails:
            raise ImapError("could not open INBOX")
        return 0

    def move(self, uid, destination):
        if type(self).move_fails:
            raise ImapError(f"could not move {uid} to {destination}")
        return "77"


@pytest.fixture
def pooled(monkeypatch):
    CountingSession.connects = 0
    CountingSession.logouts = 0
    CountingSession.alive = True
    CountingSession.sign_in_fails = False
    CountingSession.select_fails = False
    CountingSession.move_fails = False
    monkeypatch.setattr(mail_sync, "ImapSession", CountingSession)
    return CountingSession


@pytest.fixture
def probing(monkeypatch):
    """Probe on every reuse, however recently the connection was last used."""
    monkeypatch.setattr(mail_sync, "PROBE_IDLE_SECONDS", 0)


def test_a_second_operation_reuses_the_first_s_connection(pooled):
    fetch_mailbox(account(), CREDENTIAL)
    fetch_mailbox(account(), CREDENTIAL)

    assert pooled.connects == 1


def test_a_connection_the_server_hung_up_on_is_reopened(pooled, probing):
    fetch_mailbox(account(), CREDENTIAL)
    pooled.alive = False

    fetch_mailbox(account(), CREDENTIAL)

    assert pooled.connects == 2


def test_a_connection_just_used_is_not_probed(pooled):
    fetch_mailbox(account(), CREDENTIAL)
    pooled.alive = False  # a probe would report it dead and reconnect

    fetch_mailbox(account(), CREDENTIAL)

    assert pooled.connects == 1


def test_a_failed_operation_does_not_go_back_in_the_pool(pooled):
    pooled.select_fails = True
    with pytest.raises(ImapError):
        fetch_mailbox(account(), CREDENTIAL)
    pooled.select_fails = False

    fetch_mailbox(account(), CREDENTIAL)

    assert pooled.connects == 2


def test_a_move_that_fails_does_not_go_back_in_the_pool(pooled):
    # move_messages reports the failure instead of raising, so the pool cannot
    # see it -- it has to be told.
    pooled.move_fails = True
    assert move_messages(account(), CREDENTIAL, "INBOX", ["4"], "Archive").error
    pooled.move_fails = False

    fetch_mailbox(account(), CREDENTIAL)

    assert pooled.connects == 2


def test_a_connection_that_cannot_sign_in_is_logged_out(pooled):
    pooled.sign_in_fails = True

    with pytest.raises(ImapError):
        fetch_mailbox(account(), CREDENTIAL)

    assert pooled.logouts == 1


def test_an_operation_arriving_while_the_connection_is_busy_opens_its_own(pooled):
    fetch_mailbox(account(), CREDENTIAL)
    entry = mail_sync._pool[account().id]

    with entry.lock:  # as another operation mid-command would hold it
        fetch_mailbox(account(), CREDENTIAL)

    assert pooled.connects == 2
    assert pooled.logouts == 1  # the spare was closed, not pooled
    assert entry.session is not None  # the pooled connection was left alone


def test_removing_an_account_drops_its_connection(pooled):
    fetch_mailbox(account(), CREDENTIAL)
    mail_sync.close_sessions(account().id)

    fetch_mailbox(account(), CREDENTIAL)

    assert pooled.connects == 2


def test_removing_an_account_mid_operation_does_not_pool_the_connection(
    monkeypatch, pooled
):
    class ClosingSession(CountingSession):
        logouts = 0

        def search_all_uids(self):
            mail_sync.close_sessions(account().id)
            return set()

    monkeypatch.setattr(mail_sync, "ImapSession", ClosingSession)

    fetch_mailbox(account(), CREDENTIAL)

    assert mail_sync._pool[account().id].session is None
    assert ClosingSession.logouts == 1


# --- folders that state their own role (Microsoft Graph) ----------------------


def graph_account() -> Account:
    return Account(
        id=7,
        email="ada@contoso.com",
        display_name="Ada",
        imap_host="graph.microsoft.com",
        imap_port=443,
        smtp_host="graph.microsoft.com",
        smtp_port=443,
        goa_id="account_2",
        protocol="graph",
    )


GRAPH_TOKEN = Credential("ada@contoso.com", "token", "xoauth2")


def stored_folder(name: str, role: str = "", label: str = "", parent_id=None) -> Folder:
    return Folder(
        id=1,
        account_id=1,
        name=name,
        icon_name="folder-symbolic",
        parent_id=parent_id,
        role=role,
        label=label,
    )


def test_a_stated_role_wins_over_the_name():
    assert folder_role(stored_folder("AAMkSent", role="sent")) is FolderRole.SENT
    # "Archive" by name, but Graph said this one is an ordinary folder.
    assert folder_role(stored_folder("Archive", role="other")) is FolderRole.OTHER


def test_without_a_stated_role_it_is_inferred_from_the_name():
    assert folder_role(stored_folder("[Gmail]/Sent Mail")) is FolderRole.SENT


def test_folder_label_prefers_the_server_s_display_name():
    assert folder_label(stored_folder("AAMk1", label="Posteingang")) == "Posteingang"
    assert folder_label(stored_folder("Work/Acme", parent_id=3)) == "Acme"


def test_is_outgoing_reads_the_stated_role():
    assert is_outgoing(stored_folder("AAMk2", role="sent"))
    assert is_outgoing(stored_folder("AAMk3", role="drafts"))
    assert is_outgoing(stored_folder(mail_sync.OUTBOX_FOLDER))
    assert not is_outgoing(stored_folder("Sent", role="other"))


def test_sent_and_drafts_folders_are_found_by_stated_role(db_account):
    db, account_id = db_account
    sent = db.get_or_create_folder(account_id, "AAMkSent")
    db.set_folder_identity(sent.id, "sent", "Gesendete Elemente")
    drafts = db.get_or_create_folder(account_id, "AAMkDrafts")
    db.set_folder_identity(drafts.id, "drafts", "Entwürfe")

    assert sent_folder(db, account_id).id == sent.id
    assert drafts_folder(db, account_id).id == drafts.id
    assert len(db.folders_for_account(account_id)) == 2


def test_drafts_folder_creates_one_before_the_first_sync(db_account):
    db, account_id = db_account

    assert drafts_folder(db, account_id).name == mail_sync.DRAFTS_FOLDER


def test_imap_mailboxes_are_stored_shortest_name_first():
    boxes = mailboxes("Work/Acme", "Work", "INBOX")

    assert [box.name for box in creation_order(boxes)] == ["Work", "INBOX", "Work/Acme"]


def test_graph_mailboxes_keep_the_order_they_came_in():
    boxes = [
        MailboxInfo("long-inbox-id", "/", "", "Posteingang", "inbox", ""),
        MailboxInfo("s", "/", "", "Sub", "other", "long-inbox-id"),
    ]

    assert creation_order(boxes) == boxes
    assert parent_of(boxes[1]) == "long-inbox-id"
    assert parent_of(boxes[0]) == ""


def test_parent_of_an_imap_mailbox_splits_its_name():
    assert parent_of(MailboxInfo("Work/Acme", "/", "")) == "Work"


def test_icon_for_mailbox_uses_the_stated_role():
    assert icon_for_mailbox(MailboxInfo("AAMk", "/", "", "Gesendet", "sent")) == (
        "mail-send-symbolic"
    )
    assert icon_for_mailbox(MailboxInfo("Trash", "/", "")) == "user-trash-symbolic"
    assert icon_for_mailbox(MailboxInfo("[Gmail]", "/", "\\Noselect")) == (
        "folder-symbolic"
    )


# --- dispatch to Microsoft Graph ------------------------------------------------


class FakeGraphModules:
    """Stands in for the core Graph modules mail_sync calls, recording calls."""

    def __init__(self) -> None:
        self.well_known_calls = 0
        self.delta_states: list = []
        self.calls: list = []

    def well_known_ids(self, session):
        self.well_known_calls += 1
        return {"inbox": "in"}

    def list_folders(self, session, well_known):
        return [
            GraphFolder("in", "Posteingang", "", FolderRole.INBOX, 3, 120),
            GraphFolder("kid", "Projekte", "in", FolderRole.OTHER, 9, 10),
            GraphFolder("bin", "Gelöschte Elemente", "", FolderRole.TRASH, 1, 5),
        ]

    def fetch_headers(self, session, folder_id, limit, offset):
        self.calls.append(("headers", folder_id, limit, offset))
        return []

    def folder_ids(self, session, folder_id, state):
        self.delta_states.append(state)
        return DeltaState(f"link-{len(self.delta_states)}", frozenset({"m1", "m2"}))

    def fetch_mime(self, session, message_id):
        self.calls.append(("mime", message_id))
        return b"raw"

    def set_flags(self, session, ids, flag, should_add):
        self.calls.append(("flags", ids, flag, should_add))

    def move(self, session, ids, destination):
        self.calls.append(("move", ids, destination))
        return MoveOutcome(["m1"], 1, "boom")

    def send_mime(self, session, raw, recipients):
        self.calls.append(("send", raw, recipients))


@pytest.fixture
def graph(monkeypatch):
    fake = FakeGraphModules()
    for module in ("graph_folders", "graph_messages", "graph_send"):
        monkeypatch.setattr(mail_sync, module, fake)
    return fake


def test_a_graph_sync_maps_folders_counts_and_the_id_snapshot(graph):
    result = fetch_mailbox(graph_account(), GRAPH_TOKEN)

    assert result.folder == "in"
    assert result.exists == 120
    assert result.all_uids == {"m1", "m2"}
    assert result.folders[1] == MailboxInfo(
        "kid", "/", "", label="Projekte", role="other", parent="in"
    )
    # Every role folder but the one fetched; ordinary folders get no badge.
    assert result.unread_counts == {"bin": 1}
    assert graph.calls == [("headers", "in", mail_sync.RECENT_LIMIT, 0)]


def test_a_second_graph_sync_reuses_the_folder_ids_and_the_delta_link(graph):
    fetch_mailbox(graph_account(), GRAPH_TOKEN)
    fetch_mailbox(graph_account(), GRAPH_TOKEN)

    assert graph.well_known_calls == 1
    assert graph.delta_states[0] is None
    assert graph.delta_states[1].link == "link-1"


def test_closing_sessions_forgets_the_graph_state(graph):
    fetch_mailbox(graph_account(), GRAPH_TOKEN)
    mail_sync.close_sessions(graph_account().id)
    fetch_mailbox(graph_account(), GRAPH_TOKEN)

    assert graph.well_known_calls == 2
    assert graph.delta_states[1] is None


def test_an_older_graph_page_skips_the_snapshot_and_the_counts(graph):
    result = fetch_mailbox(graph_account(), GRAPH_TOKEN, "kid", offset=50)

    assert result.all_uids is None
    assert result.unread_counts == {}
    assert graph.delta_states == []


def test_graph_operations_go_to_graph(graph, monkeypatch):
    def no_imap(*_args, **_kwargs):
        raise AssertionError("a Graph account must not open IMAP or SMTP")

    monkeypatch.setattr(mail_sync, "ImapSession", no_imap)
    monkeypatch.setattr(mail_sync, "SmtpSession", no_imap)
    account = graph_account()

    assert fetch_full_message(account, GRAPH_TOKEN, "in", "m1") == b"raw"
    mail_sync.set_flag(account, GRAPH_TOKEN, "in", ("m1",), FLAG_SEEN, True)
    moved = move_messages(account, GRAPH_TOKEN, "in", ["m1", "m2"], "bin")
    send_message(account, GRAPH_TOKEN, account.email, ["x@y"], b"raw")

    assert moved == mail_sync.MoveResult(["m1"], 1, "boom")
    assert graph.calls == [
        ("mime", "m1"),
        ("flags", ["m1"], FLAG_SEEN, True),
        ("move", ["m1", "m2"], "bin"),
        ("send", b"raw", ["x@y"]),
    ]
