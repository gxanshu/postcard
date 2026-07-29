from postcard.core.models.email import Email
from postcard.core.threader import _normalize_subject, group


def mail(
    id: int,
    subject: str = "",
    message_id: str = "",
    in_reply_to: str = "",
    references: str = "",
) -> Email:
    return Email(
        id=id,
        folder_id=1,
        server_id=str(id),
        sender="someone@example.com",
        subject=subject,
        preview="",
        date="",
        unread=False,
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references,
    )


def test_group_of_nothing():
    assert group([]) == {}


def test_lone_email_is_its_own_conversation():
    assert group([mail(7, message_id="<a>")]) == {7: 7}


def test_in_reply_to_links_a_reply_to_its_parent():
    emails = [mail(1, message_id="<a>"), mail(2, message_id="<b>", in_reply_to="<a>")]
    assert group(emails) == {1: 1, 2: 1}


def test_references_chain_merges_transitively():
    emails = [
        mail(4, message_id="<a>"),
        mail(7, message_id="<b>"),
        mail(9, message_id="<c>", references="<a> <b>"),
    ]
    assert group(emails) == {4: 4, 7: 4, 9: 4}


def test_conversation_id_is_the_smallest_id_not_the_first_seen():
    emails = [mail(5, subject="Hi"), mail(2, subject="Re: Hi")]
    assert group(emails) == {5: 2, 2: 2}


def test_grouping_does_not_depend_on_input_order():
    a, b, c = (
        mail(3, message_id="<c>", in_reply_to="<a>"),
        mail(1, message_id="<a>"),
        mail(2, message_id="<b>", in_reply_to="<a>"),
    )
    assert group([a, b, c]) == group([c, b, a]) == {1: 1, 2: 1, 3: 1}


def test_subject_fallback_threads_unrelated_message_ids():
    emails = [mail(1, subject="Lunch", message_id="<a>"), mail(2, subject="Re: Lunch")]
    assert group(emails) == {1: 1, 2: 1}


def test_repeated_reply_prefixes_normalize_to_the_same_subject():
    emails = [mail(1, subject="Deal"), mail(2, subject="RE:RE: Deal")]
    assert group(emails) == {1: 1, 2: 1}


def test_placeholder_subjects_never_merge():
    emails = [mail(1, subject="(no subject)"), mail(2, subject="")]
    assert group(emails) == {1: 1, 2: 2}


def test_emails_without_a_message_id_stay_apart():
    # Each gets a synthetic "eid:<id>" token rather than sharing the empty one.
    assert group([mail(1), mail(2)]) == {1: 1, 2: 2}


def test_two_orphans_replying_to_a_missing_parent_still_thread():
    emails = [
        mail(1, message_id="<a>", in_reply_to="<gone>"),
        mail(2, message_id="<b>", in_reply_to="<gone>"),
    ]
    assert group(emails) == {1: 1, 2: 1}


def test_same_message_id_in_two_folders_threads_together():
    # Gmail shows one message in both Inbox and All Mail.
    assert group([mail(1, message_id="<a>"), mail(8, message_id="<a>")]) == {1: 1, 8: 1}


def test_normalize_subject():
    assert _normalize_subject("Re: Hello") == "hello"
    assert _normalize_subject("RE:RE: Hello") == "hello"
    assert _normalize_subject("  fwd:  Fw: Deal  ") == "deal"
    assert _normalize_subject("(No Subject)") == ""
    assert _normalize_subject("   ") == ""
    assert _normalize_subject("Fwd:Fwd:") == ""
    # "Reply:" is not a reply prefix — only re/fwd/fw followed by a colon.
    assert _normalize_subject("Reply: x") == "reply: x"
