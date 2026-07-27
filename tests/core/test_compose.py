from postcard.core.compose import (
    build_mime_message,
    extract_recipients,
    forward_body,
    forward_subject,
    html_to_text,
    quote_reply_body,
    replace_last_address,
    reply_subject,
    signature_block,
    suggest_addresses,
)
from postcard.core.models.attachment import Attachment


def build(to=None, cc=None, subject="Hi", body="<p>Hi</p>", attachments=None):
    return build_mime_message(
        from_addr="me@example.com",
        to_addrs=to if to is not None else ["you@example.com"],
        cc_addrs=cc if cc is not None else [],
        subject=subject,
        body_html=body,
        attachments=attachments if attachments is not None else [],
    )


# --- subjects ---------------------------------------------------------------


def test_reply_subject_adds_a_prefix():
    assert reply_subject("Lunch") == "Re: Lunch"


def test_reply_subject_leaves_an_existing_prefix_alone():
    assert reply_subject("Re: Lunch") == "Re: Lunch"
    assert reply_subject("RE: Lunch") == "RE: Lunch"


def test_forward_subject_adds_a_prefix():
    assert forward_subject("Lunch") == "Fwd: Lunch"


def test_forward_subject_leaves_an_existing_prefix_alone():
    assert forward_subject("Fwd: Lunch") == "Fwd: Lunch"
    assert forward_subject("FWD: Lunch") == "FWD: Lunch"
    # "Fw:" is a forward marker too — threader._REPLY_PREFIX already strips it.
    assert forward_subject("Fw: Lunch") == "Fw: Lunch"


# --- html_to_text -----------------------------------------------------------


def test_html_to_text_puts_a_blank_line_between_paragraphs():
    assert html_to_text("<p>Hello</p><p>World</p>") == "Hello\n\nWorld"


def test_html_to_text_bullets_list_items():
    assert html_to_text("<ul><li>a</li><li>b</li></ul>") == "- a\n- b"


def test_html_to_text_breaks_on_br():
    assert html_to_text("a<br>b") == "a\nb"


def test_html_to_text_drops_script_and_style_content():
    assert html_to_text("<style>p{}</style><script>bad()</script>ok") == "ok"


def test_html_to_text_decodes_entities_and_trims():
    assert html_to_text("  <div>&amp;&lt;</div>  ") == "&<"


def test_html_to_text_collapses_runs_of_blank_lines():
    assert html_to_text("<p></p><p></p><p>x</p>") == "x"


def test_html_to_text_of_nothing():
    assert html_to_text("") == ""


# --- quoting ----------------------------------------------------------------


def test_signature_block_uses_the_rfc3676_delimiter_and_escapes():
    assert signature_block("a\nb<") == '<div class="signature">-- <br>a<br>b&lt;</div>'


def test_quote_reply_body_escapes_the_attribution_and_quotes_the_body():
    assert quote_reply_body("A <a@b>", "Jul 1", "line1\nline2") == (
        "<div><br></div>"
        "<div>On Jul 1, A &lt;a@b&gt; wrote:</div>"
        "<blockquote>line1<br>line2</blockquote>"
    )


def test_quote_reply_body_inserts_the_signature_above_the_quote():
    body = quote_reply_body("A", "D", "T", signature="Sig")
    assert body.index('class="signature"') < body.index("wrote:")


def test_quote_reply_body_without_a_signature_has_no_signature_div():
    assert "signature" not in quote_reply_body("A", "D", "T")


def test_forward_body_includes_the_original_headers():
    body = forward_body("A <a@b>", "Jul 1", "Lunch", "T")
    assert "---------- Forwarded message ----------" in body
    assert "From: A &lt;a@b&gt;" in body
    assert "Date: Jul 1" in body
    assert "Subject: Lunch" in body
    assert "<blockquote>T</blockquote>" in body


# --- build_mime_message -----------------------------------------------------
# Date, Message-ID and MIME boundaries are generated, so assert on structure.


def test_build_mime_message_sets_the_basic_headers():
    msg = build(to=["a@x.com", "b@x.com"], subject="Lunch")
    assert msg["From"] == "me@example.com"
    assert msg["To"] == "a@x.com, b@x.com"
    assert msg["Subject"] == "Lunch"
    assert msg["Date"] and msg["Message-ID"]


def test_build_mime_message_offers_plain_text_before_html():
    msg = build(body="<p>Hello</p>")
    assert msg.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in msg.iter_parts()] == [
        "text/plain",
        "text/html",
    ]
    plain = msg.get_body(("plain",))
    assert plain is not None
    assert plain.get_content().strip() == "Hello"


def test_build_mime_message_omits_cc_when_there_is_none():
    assert build(cc=[])["Cc"] is None


def test_build_mime_message_sets_cc_when_there_is_some():
    assert build(cc=["c@x.com", "d@x.com"])["Cc"] == "c@x.com, d@x.com"


def test_build_mime_message_never_writes_a_bcc_header():
    # Bcc is passed to the SMTP envelope, never into the message itself.
    assert build()["Bcc"] is None


def test_build_mime_message_attaches_files():
    msg = build(attachments=[Attachment("notes.txt", "text/plain", b"hello")])
    assert msg.get_content_type() == "multipart/mixed"
    (part,) = msg.iter_attachments()
    assert part.get_filename() == "notes.txt"
    assert part.get_payload(decode=True) == b"hello"


def test_build_mime_message_keeps_a_well_formed_mime_type():
    msg = build(attachments=[Attachment("a.png", "image/png", b"PNG")])
    (part,) = msg.iter_attachments()
    assert part.get_content_type() == "image/png"


def test_build_mime_message_falls_back_for_an_empty_mime_type():
    msg = build(attachments=[Attachment("blob", "", b"x")])
    (part,) = msg.iter_attachments()
    assert part.get_content_type() == "application/octet-stream"


def test_build_mime_message_falls_back_for_a_mime_type_without_a_slash():
    msg = build(attachments=[Attachment("notes", "pdf", b"x")])
    (part,) = msg.iter_attachments()
    assert part.get_content_type() == "application/octet-stream"


# --- recipients -------------------------------------------------------------


def test_extract_recipients_returns_addresses_to_before_cc():
    raw = b"To: a@x.com, B Person <b@x.com>\nCc: c@x.com\n\nbody"
    assert extract_recipients(raw) == ["a@x.com", "b@x.com", "c@x.com"]


def test_extract_recipients_without_recipient_headers():
    assert extract_recipients(b"Subject: hi\n\nbody") == []


def test_suggest_addresses_matches_the_last_entry_case_insensitively():
    known = ["bob@x.com", "BOB2@X.com", "carl@x.com"]
    assert suggest_addresses("a@x.com, bo", known) == ["bob@x.com", "BOB2@X.com"]


def test_suggest_addresses_needs_something_typed():
    assert suggest_addresses("a@x.com, ", ["bob@x.com"]) == []
    assert suggest_addresses("", ["bob@x.com"]) == []


def test_suggest_addresses_honours_the_limit():
    assert suggest_addresses("x", ["a@x", "b@x", "c@x"], limit=2) == ["a@x", "b@x"]


def test_replace_last_address_swaps_the_entry_being_typed():
    assert replace_last_address("a@x.com, bo", "bob@x.com") == "a@x.com, bob@x.com, "


def test_replace_last_address_on_the_first_entry():
    assert replace_last_address("bo", "bob@x.com") == "bob@x.com, "
