import pytest

from postcard.core.models.email import Email
from postcard.core.models.message_header import MessageHeader
from postcard.core.store.database import Database, _arrival_key, _fts_query


@pytest.fixture
def db():
    # An explicit path keeps the constructor off XDG_DATA_HOME entirely.
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def folder(db):
    account = db.save_account(
        "me@example.com", "Me", "imap.example.com", 993, "smtp.example.com", 587
    )
    return db.get_or_create_folder(account.id, "INBOX")


def _email(*, server_id: str | None) -> Email:
    """A bare Email, for the pure helpers that only read one field off it."""
    return Email(
        id=1,
        folder_id=1,
        server_id=server_id,
        sender="a@x",
        subject="s",
        preview="",
        date="",
        is_unread=False,
    )


def incoming(db, folder_id, uid, subject="Lunch", **kwargs):
    kwargs.setdefault("sender", "Ada <ada@example.com>")
    kwargs.setdefault("sender_address", "")
    kwargs.setdefault("recipient", "")
    kwargs.setdefault("recipient_address", "")
    kwargs.setdefault("preview", "see you at one")
    kwargs.setdefault("date", "Jul 16")
    kwargs.setdefault("is_unread", True)
    header = MessageHeader(uid=uid, subject=subject, **kwargs)
    return db.save_incoming_email(folder_id, header)


# --- accounts ---------------------------------------------------------------


def test_save_and_list_accounts(db):
    saved = db.save_account(
        "me@example.com", "Me", "imap.example.com", 993, "smtp.example.com", 587
    )
    (listed,) = db.accounts()
    assert (listed.id, listed.email, listed.display_name) == (
        saved.id,
        saved.email,
        "Me",
    )
    assert (listed.imap_host, listed.imap_port) == ("imap.example.com", 993)
    assert (listed.smtp_host, listed.smtp_port) == ("smtp.example.com", 587)


def test_smtp_security_is_inferred_from_the_port(db):
    implicit = db.save_account("a@x", "A", "imap.x", 993, "smtp.x", 465)
    starttls = db.save_account("b@x", "B", "imap.x", 993, "smtp.x", 587)
    assert implicit.smtp_security == "tls"
    assert starttls.smtp_security == "starttls"


def test_an_explicit_smtp_security_wins(db):
    account = db.save_account(
        "a@x", "A", "imap.x", 993, "smtp.x", 465, smtp_security="starttls"
    )
    assert account.smtp_security == "starttls"


def test_deleting_an_account_takes_its_folders_and_emails(db, folder):
    incoming(db, folder.id, "1")
    account_id = db.accounts()[0].id

    db.delete_account(account_id)

    assert db.accounts() == []
    assert db.folders_for_account(account_id) == []
    assert db.emails_in_folder(folder.id) == []


# --- folders ----------------------------------------------------------------


def test_get_or_create_folder_is_idempotent(db):
    account = db.save_account("a@x", "A", "imap.x", 993, "smtp.x", 587)
    first = db.get_or_create_folder(account.id, "Sent")
    second = db.get_or_create_folder(account.id, "Sent")
    assert first.id == second.id
    assert len(db.folders_for_account(account.id)) == 1


def test_get_folder_by_name_when_absent(db):
    account = db.save_account("a@x", "A", "imap.x", 993, "smtp.x", 587)
    assert db.get_folder_by_name(account.id, "Nope") is None


def test_prune_folders_deletes_a_whole_subtree_and_its_mail(db, folder):
    account_id = folder.account_id
    parent = db.get_or_create_folder(account_id, "Work")
    child = db.get_or_create_folder(account_id, "Work/2026")
    db.set_folder_parent(child.id, parent.id, "/")
    incoming(db, child.id, "1")

    db.prune_folders(account_id, {"INBOX"})

    assert [f.name for f in db.folders_for_account(account_id)] == ["INBOX"]
    assert db.emails_in_folder(child.id) == []


def test_prune_folders_keeps_what_the_server_still_lists(db, folder):
    account_id = folder.account_id
    db.get_or_create_folder(account_id, "Sent")
    db.get_or_create_folder(account_id, "Gone")

    db.prune_folders(account_id, {"INBOX", "Sent"})

    assert sorted(f.name for f in db.folders_for_account(account_id)) == [
        "INBOX",
        "Sent",
    ]


# --- emails -----------------------------------------------------------------


def test_save_incoming_email_reports_whether_it_was_new(db, folder):
    assert incoming(db, folder.id, "1") is True
    assert incoming(db, folder.id, "1") is False
    assert len(db.emails_in_folder(folder.id)) == 1


def test_resyncing_a_message_takes_the_servers_flags(db, folder):
    incoming(db, folder.id, "1", is_unread=True, is_starred=False)
    incoming(db, folder.id, "1", is_unread=False, is_starred=True)

    (mail,) = db.emails_in_folder(folder.id)
    assert (mail.is_unread, mail.is_starred) == (False, True)


def test_resyncing_a_message_leaves_the_rest_of_it_alone(db, folder):
    incoming(db, folder.id, "1", subject="Lunch", preview="see you at one")
    incoming(db, folder.id, "1", subject="", preview="", sender="")

    (mail,) = db.emails_in_folder(folder.id)
    assert (mail.subject, mail.preview) == ("Lunch", "see you at one")


def test_the_sender_address_round_trips_separately_from_the_display_name(db, folder):
    incoming(db, folder.id, "1", sender="Ada", sender_address="ada@example.com")
    (mail,) = db.emails_in_folder(folder.id)
    assert (mail.sender, mail.sender_address) == ("Ada", "ada@example.com")


def test_mail_saved_without_a_sender_address_reads_back_as_empty(db, folder):
    # Rows predating the column, and locally-saved copies, have no address.
    incoming(db, folder.id, "1")
    (mail,) = db.emails_in_folder(folder.id)
    assert mail.sender_address == ""


def test_the_same_uid_in_another_folder_is_a_different_message(db, folder):
    other = db.get_or_create_folder(folder.account_id, "Archive")
    assert incoming(db, folder.id, "1") is True
    assert incoming(db, other.id, "1") is True


def test_locally_saved_emails_without_a_uid_are_never_deduped(db, folder):
    # SQLite treats NULLs as distinct, so the unique index doesn't collapse these.
    db.save_email(folder.id, "me@x", "Draft", "", "Jul 16", False)
    db.save_email(folder.id, "me@x", "Draft", "", "Jul 16", False)
    assert len(db.emails_in_folder(folder.id)) == 2


@pytest.mark.parametrize(
    ("active_server_uids", "local_server_id", "deleted"),
    [({"active"}, None, 1), (set(), "", 2)],
)
def test_prune_stale_emails_keeps_active_and_uid_less_rows(
    db, folder, active_server_uids, local_server_id, deleted
):
    incoming(db, folder.id, "active", subject="Active")
    incoming(db, folder.id, "stale", subject="Stale")
    db.save_email(folder.id, "me@x", "Draft", "", "Jul 16", False, local_server_id)

    assert db.prune_stale_emails(folder.id, active_server_uids) == deleted

    assert {mail.server_id for mail in db.emails_in_folder(folder.id)} == (
        active_server_uids | {local_server_id}
    )


def test_prune_stale_emails_accepts_an_authoritative_set_over_sqlite_variable_limit(
    db, folder
):
    incoming(db, folder.id, "active", subject="Active")
    incoming(db, folder.id, "stale", subject="Stale")
    active_server_uids = {f"uid-{uid}" for uid in range(250_000)} | {"active"}

    assert db.prune_stale_emails(folder.id, active_server_uids) == 1

    assert [mail.server_id for mail in db.emails_in_folder(folder.id)] == ["active"]


def test_the_recipient_of_a_synced_message_round_trips(db, folder):
    incoming(db, folder.id, "1", recipient="Bob", recipient_address="bob@example.com")

    (mail,) = db.emails_in_folder(folder.id)
    assert (mail.recipient, mail.recipient_address) == ("Bob", "bob@example.com")


def test_re_syncing_fills_in_a_recipient_the_stored_row_never_had(db, folder):
    incoming(db, folder.id, "1", recipient="", recipient_address="")

    incoming(db, folder.id, "1", recipient="Bob", recipient_address="bob@example.com")

    (mail,) = db.emails_in_folder(folder.id)
    assert (mail.recipient, mail.recipient_address) == ("Bob", "bob@example.com")


def test_a_locally_saved_message_keeps_its_recipient(db, folder):
    db.save_email(
        folder.id,
        sender="me@example.com",
        recipient="Bob",
        recipient_address="bob@example.com",
        subject="Lunch",
        preview="",
        date="",
        is_unread=False,
    )

    (mail,) = db.emails_in_folder(folder.id)
    assert (mail.recipient, mail.recipient_address) == ("Bob", "bob@example.com")


def test_the_python_flags_map_onto_the_original_column_names(db, folder):
    # The Email/MessageHeader fields are is_unread/is_starred but the columns
    # are still `unread`/`starred`, so _email_from_row bridges the two. A rename
    # leaking into the SQL would only show up at runtime.
    incoming(db, folder.id, "1", is_unread=True, is_starred=True)

    (mail,) = db.emails_in_folder(folder.id)
    assert (mail.is_unread, mail.is_starred) == (True, True)

    columns = {row[1] for row in db._conn.execute("PRAGMA table_info(emails)")}
    assert {"unread", "starred"} <= columns
    assert not {"is_unread", "is_starred"} & columns


def test_read_and_unread(db, folder):
    incoming(db, folder.id, "1", is_unread=True)
    (email,) = db.emails_in_folder(folder.id)
    assert db.unread_count_in_folder(folder.id) == 1

    db.mark_email_read(email.id)
    assert db.unread_count_in_folder(folder.id) == 0

    db.mark_email_unread(email.id)
    assert db.unread_count_in_folder(folder.id) == 1


def test_starring(db, folder):
    incoming(db, folder.id, "1")
    (email,) = db.emails_in_folder(folder.id)
    assert email.is_starred is False

    db.set_email_starred(email.id, True)
    assert db.emails_in_folder(folder.id)[0].is_starred is True


def test_moving_an_email_between_folders(db, folder):
    trash = db.get_or_create_folder(folder.account_id, "Trash")
    incoming(db, folder.id, "1")
    (email,) = db.emails_in_folder(folder.id)

    db.move_email(email.id, trash.id)

    assert db.emails_in_folder(folder.id) == []
    assert [e.id for e in db.emails_in_folder(trash.id)] == [email.id]


def test_deleting_an_email(db, folder):
    incoming(db, folder.id, "1")
    (email,) = db.emails_in_folder(folder.id)
    db.delete_email(email.id)
    assert db.emails_in_folder(folder.id) == []


def test_raw_message_round_trip(db, folder):
    incoming(db, folder.id, "1")
    (email,) = db.emails_in_folder(folder.id)
    assert db.get_raw_message(email.id) is None

    db.save_raw_message(email.id, b"From: a@x\n\nbody")
    assert db.get_raw_message(email.id) == b"From: a@x\n\nbody"


# --- ordering & threading ---------------------------------------------------


def test_arrival_key_uses_the_imap_uid():
    assert _arrival_key(_email(server_id="42")) == 42


def test_arrival_key_sorts_uid_less_messages_newest():
    # A Sent copy saved locally has no UID until the next sync confirms it.
    newest = 2**31 - 1
    assert _arrival_key(_email(server_id=None)) == newest
    assert _arrival_key(_email(server_id="")) == newest


def test_conversations_are_newest_first_by_uid_not_by_local_id(db, folder):
    # Load-on-scroll backfill gives *older* mail a *newer* local id.
    incoming(db, folder.id, "9", subject="Newer")
    incoming(db, folder.id, "2", subject="Older")

    assert [c.subject for c in db.conversations_in_folder(folder.id)] == [
        "Newer",
        "Older",
    ]


def test_reassign_conversations_groups_a_reply_with_its_parent(db, folder):
    incoming(db, folder.id, "1", subject="Lunch", message_id="<a>")
    incoming(
        db, folder.id, "2", subject="Re: Lunch", message_id="<b>", in_reply_to="<a>"
    )

    db.reassign_conversations(folder.id)

    (conversation,) = db.conversations_in_folder(folder.id)
    assert conversation.count == 2
    # Sorted oldest first, so .latest is genuinely the newest message.
    assert conversation.latest.subject == "Re: Lunch"


def test_unthreaded_emails_stay_separate_conversations(db, folder):
    incoming(db, folder.id, "1", subject="Lunch", message_id="<a>")
    incoming(db, folder.id, "2", subject="Invoice", message_id="<b>")

    db.reassign_conversations(folder.id)

    assert len(db.conversations_in_folder(folder.id)) == 2


# --- search -----------------------------------------------------------------


def test_fts_query_prefix_matches_every_word():
    assert _fts_query("hello world") == '"hello"* "world"*'


def test_fts_query_doubles_embedded_quotes():
    assert _fts_query('say "hi"') == '"say"* """hi"""*'


def test_fts_query_of_blank_text():
    assert _fts_query("   ") == ""


def test_search_matches_a_subject_prefix(db, folder):
    incoming(db, folder.id, "1", subject="Lunch plans")
    incoming(db, folder.id, "2", subject="Invoice")

    results = db.search_conversations(folder.id, "lun")

    assert [c.subject for c in results] == ["Lunch plans"]


def test_search_requires_every_word_to_match(db, folder):
    incoming(db, folder.id, "1", subject="Lunch plans")

    assert db.search_conversations(folder.id, "lunch plans") != []
    assert db.search_conversations(folder.id, "lunch invoice") == []


def test_search_matches_the_sender_and_preview(db, folder):
    incoming(db, folder.id, "1", subject="Lunch")
    assert db.search_conversations(folder.id, "ada") != []
    assert db.search_conversations(folder.id, "one") != []


def test_an_empty_search_returns_everything(db, folder):
    incoming(db, folder.id, "1")
    incoming(db, folder.id, "2", subject="Invoice")

    assert len(db.search_conversations(folder.id, "")) == 2
    assert len(db.search_conversations(folder.id, "   ")) == 2


def test_a_quoted_search_term_does_not_break_fts(db, folder):
    incoming(db, folder.id, "1", subject="Lunch")
    assert db.search_conversations(folder.id, 'say "hi"') == []


def test_matching_one_email_returns_its_whole_conversation(db, folder):
    incoming(db, folder.id, "1", subject="Lunch", message_id="<a>")
    incoming(
        db,
        folder.id,
        "2",
        subject="Re: Lunch",
        message_id="<b>",
        in_reply_to="<a>",
        preview="bringing dessert",
    )
    db.reassign_conversations(folder.id)

    (conversation,) = db.search_conversations(folder.id, "dessert")

    assert conversation.count == 2


# --- contacts ---------------------------------------------------------------


def test_save_contacts_lowercases_and_formats(db):
    db.save_contacts([("Ada L", "Ada@Example.COM"), ("", "bob@example.com")])
    assert db.contact_addresses() == ["bob@example.com", "Ada L <ada@example.com>"]


def test_a_later_anonymous_sighting_never_wipes_a_known_name(db):
    db.save_contacts([("Ada L", "ada@example.com")])
    db.save_contacts([("", "ada@example.com")])
    assert db.contact_addresses() == ["Ada L <ada@example.com>"]


def test_a_later_sighting_fills_in_a_missing_name(db):
    db.save_contacts([("", "ada@example.com")])
    db.save_contacts([("Ada L", "ada@example.com")])
    assert db.contact_addresses() == ["Ada L <ada@example.com>"]


def test_save_contacts_skips_entries_with_no_address(db):
    db.save_contacts([("Nobody", "")])
    assert db.contact_addresses() == []
