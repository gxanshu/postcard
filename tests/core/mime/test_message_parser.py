from postcard.core.mime.message_parser import (
    Unsubscribe,
    _format_date,
    parse_message,
    sandbox_html,
)

PLAIN = b"""\
From: Ada <ada@example.com>
To: bob@example.com, C Person <c@example.com>
Cc: d@example.com
Subject: Lunch
Date: Wed, 16 Jul 2026 10:00:00 +0000
Content-Type: text/plain; charset="utf-8"

hello body
"""

ALTERNATIVE = b"""\
From: ada@example.com
Subject: Both
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="B"

--B
Content-Type: text/plain

text version
--B
Content-Type: text/html

<p>html version</p>
--B--
"""


def mixed(disposition: bytes) -> bytes:
    return (
        b"From: ada@example.com\nSubject: With file\nMIME-Version: 1.0\n"
        b'Content-Type: multipart/mixed; boundary="B"\n\n'
        b"--B\nContent-Type: text/plain\n\nbody\n"
        b"--B\nContent-Type: image/png\n"
        b"Content-Transfer-Encoding: base64\n" + disposition + b"\n"
        b"iVBORw==\n"
        b"--B--\n"
    )


def test_parses_headers_and_a_plain_body():
    parsed = parse_message(PLAIN)
    assert parsed.subject == "Lunch"
    assert parsed.from_display == "Ada <ada@example.com>"
    assert parsed.to == ["bob@example.com", "C Person <c@example.com>"]
    assert parsed.cc == ["d@example.com"]
    assert parsed.text_body == "hello body\n"
    assert parsed.html_body is None


def test_multipart_alternative_fills_both_bodies():
    parsed = parse_message(ALTERNATIVE)
    assert parsed.text_body == "text version"
    assert parsed.html_body == "<p>html version</p>"
    assert parsed.attachments == []


def test_multipart_mixed_collects_the_attachment():
    parsed = parse_message(mixed(b'Content-Disposition: attachment; filename="a.png"'))
    assert parsed.text_body == "body"
    (attachment,) = parsed.attachments
    assert attachment.filename == "a.png"
    assert attachment.mime_type == "image/png"
    assert attachment.size == len(attachment.content)


def test_an_inline_part_with_no_disposition_still_becomes_an_attachment():
    parsed = parse_message(mixed(b"Content-ID: <img1>"))
    (attachment,) = parsed.attachments
    assert attachment.mime_type == "image/png"
    # No filename to take from the headers, so it gets the placeholder.
    assert attachment.filename == "attachment"


def test_disposition_attachment_beats_the_content_type():
    raw = (
        b"From: a@x\nMIME-Version: 1.0\n"
        b'Content-Type: multipart/mixed; boundary="B"\n\n'
        b"--B\nContent-Type: text/plain\n\nbody\n"
        b"--B\nContent-Type: text/plain\n"
        b'Content-Disposition: attachment; filename="notes.txt"\n\nattached\n'
        b"--B--\n"
    )
    parsed = parse_message(raw)
    assert parsed.text_body == "body"
    assert [a.filename for a in parsed.attachments] == ["notes.txt"]


def test_a_second_text_part_falls_through_to_attachments():
    raw = (
        b"From: a@x\nMIME-Version: 1.0\n"
        b'Content-Type: multipart/mixed; boundary="B"\n\n'
        b"--B\nContent-Type: text/plain\n\nfirst\n"
        b"--B\nContent-Type: text/plain\n\nsecond\n"
        b"--B--\n"
    )
    parsed = parse_message(raw)
    assert parsed.text_body == "first"
    assert [a.content.strip() for a in parsed.attachments] == [b"second"]


def test_encoded_word_headers_are_decoded():
    raw = (
        b"From: =?utf-8?q?Ren=C3=A9?= <r@x.com>\n"
        b"Subject: =?utf-8?q?Caf=C3=A9?=\n\nbody\n"
    )
    parsed = parse_message(raw)
    assert parsed.subject == "Café"
    assert parsed.from_display == "René <r@x.com>"


def test_a_non_utf8_charset_is_decoded():
    raw = b'Content-Type: text/plain; charset="iso-8859-1"\n\ncaf\xe9\n'
    assert parse_message(raw).text_body == "café\n"


def test_format_date():
    assert _format_date("Wed, 16 Jul 2026 10:00:00 +0000") == "Jul 16, 2026 10:00"


def test_format_date_passes_an_unparseable_value_through():
    assert _format_date("sometime last week") == "sometime last week"


def test_format_date_of_a_missing_header():
    assert _format_date(None) == ""
    assert parse_message(b"Subject: no date\n\nbody\n").date == ""


def test_empty_and_garbage_input_do_not_raise():
    assert parse_message(b"").attachments == []
    assert parse_message(b"\x00\xff not a message at all").attachments == []


def test_sandbox_html_blocks_remote_subresources() -> None:
    blocked = sandbox_html("<p>hi</p>", are_remote_images_allowed=False)
    assert "default-src 'none'" in blocked
    assert 'img-src data:"' in blocked
    assert "<p>hi</p>" in blocked

    allowed = sandbox_html("<p>hi</p>", are_remote_images_allowed=True)
    assert "img-src data: https: http:" in allowed
    # Remote CSS stays blocked either way -- it leaks the read like a pixel does.
    assert "style-src 'unsafe-inline';" in allowed


ONE_CLICK = b"""\
From: news@acme.com
Subject: Weekly
List-Unsubscribe: <mailto:leave@acme.com>, <https://acme.com/u/abc>
List-Unsubscribe-Post: List-Unsubscribe=One-Click

body
"""


def unsubscribe(raw: bytes) -> Unsubscribe:
    target = parse_message(raw).unsubscribe
    assert target is not None
    return target


def test_one_click_unsubscribe_keeps_both_targets():
    target = unsubscribe(ONE_CLICK)
    assert target.url == "https://acme.com/u/abc"
    assert target.mailto == "mailto:leave@acme.com"
    assert target.is_one_click


def test_one_click_needs_https_and_the_post_header():
    plaintext = ONE_CLICK.replace(b"https://", b"http://")
    assert unsubscribe(plaintext).is_one_click is False

    no_post = ONE_CLICK.replace(
        b"List-Unsubscribe-Post: List-Unsubscribe=One-Click\n", b""
    )
    assert unsubscribe(no_post).is_one_click is False


def test_unsubscribe_ignores_a_scheme_we_would_never_open():
    hostile = (
        b"List-Unsubscribe: <file:///etc/passwd>, <mailto:leave@acme.com>\n\nbody\n"
    )
    target = unsubscribe(hostile)
    assert target.url == ""
    assert target.mailto == "mailto:leave@acme.com"


def test_a_message_from_no_mailing_list_has_no_unsubscribe():
    assert parse_message(PLAIN).unsubscribe is None
