import base64
import email
from email import policy

import pytest

from postcard.core.compose import build_mime_message
from postcard.core.models.attachment import Attachment
from postcard.core.net.graph_send import (
    bcc_recipients,
    send_mime,
    split_attachments,
    with_bcc,
)
from postcard.core.net.graph_session import GraphError, Response


def message(attachments=()) -> bytes:
    return build_mime_message(
        "ada@example.com",
        ["grace@example.com"],
        ["Carol <carol@example.com>"],
        "Hello",
        "<p>Hi</p>",
        list(attachments),
    ).as_bytes()


def test_bcc_recipients_are_the_ones_the_headers_do_not_name():
    raw = message()

    assert bcc_recipients(
        raw, ["grace@example.com", "CAROL@example.com", "eve@example.com"]
    ) == ["eve@example.com"]


def test_with_bcc_prepends_a_header_and_leaves_the_rest_alone():
    raw = message()

    outgoing = with_bcc(raw, ["eve@example.com", "mallory@example.com"])

    assert outgoing.endswith(raw)
    parsed = email.message_from_bytes(outgoing, policy=policy.default)
    assert parsed["Bcc"] == "eve@example.com, mallory@example.com"


def test_with_bcc_matches_crlf_line_endings():
    assert (
        with_bcc(b"From: a\r\n\r\nbody", ["e@x"]) == b"Bcc: e@x\r\nFrom: a\r\n\r\nbody"
    )


def test_no_bcc_changes_nothing():
    raw = message()
    assert with_bcc(raw, []) is raw


def test_split_attachments_detaches_files_and_keeps_the_body():
    raw = message(
        [
            Attachment(
                filename="report.pdf", mime_type="application/pdf", content=b"%PDF"
            )
        ]
    )

    body, detached = split_attachments(raw)

    assert [(a.filename, a.content_type, a.content) for a in detached] == [
        ("report.pdf", "application/pdf", b"%PDF")
    ]
    parsed = email.message_from_bytes(body, policy=policy.default)
    assert list(parsed.iter_attachments()) == []
    assert parsed["Message-ID"] == email.message_from_bytes(raw)["Message-ID"]
    assert b"Hi" in body


def test_split_attachments_of_a_plain_message_is_a_no_op():
    raw = b"From: a@x\n\nbody"
    assert split_attachments(raw) == (raw, [])


class FakeGraph:
    def __init__(self, *failures):
        self.calls = []
        self._failures = dict(failures)

    def request(
        self, method, path, body=None, content_type="application/json", prefer=""
    ):
        self.calls.append((method, path))
        failure = self._failures.get((method, path))
        if failure is not None:
            raise failure
        if (method, path) == ("POST", "/me/messages"):
            self.draft_mime = base64.b64decode(body or b"")
            return Response(201, b'{"id": "draft/1"}')
        return Response(202)

    def send(self, method, path, payload=None):
        self.calls.append((method, path))
        return {"uploadUrl": "https://upload"}

    def upload(self, url, data):
        self.calls.append(("PUT", url))


def test_send_mime_posts_the_message_with_its_bcc():
    graph = FakeGraph()

    send_mime(graph, message(), ["grace@example.com", "eve@example.com"])  # type: ignore[arg-type]

    assert graph.calls == [("POST", "/me/sendMail")]


def test_a_message_too_large_for_one_request_goes_out_as_a_draft():
    graph = FakeGraph((("POST", "/me/sendMail"), GraphError(413, "tooLarge", "big")))
    small = Attachment(filename="a.txt", mime_type="text/plain", content=b"a")
    large = Attachment(
        filename="b.bin", mime_type="application/octet-stream", content=b"b" * 3_500_000
    )

    send_mime(graph, message([small, large]), ["eve@example.com"])  # type: ignore[arg-type]

    assert graph.calls == [
        ("POST", "/me/sendMail"),
        ("POST", "/me/messages"),
        ("POST", "/me/messages/draft%2F1/attachments"),
        ("POST", "/me/messages/draft%2F1/attachments/createUploadSession"),
        ("PUT", "https://upload"),
        ("POST", "/me/messages/draft%2F1/send"),
    ]
    assert b"Bcc: eve@example.com" in graph.draft_mime
    assert len(graph.draft_mime) < 10_000


def test_a_draft_that_cannot_be_sent_is_removed_again():
    graph = FakeGraph(
        (("POST", "/me/sendMail"), GraphError(413, "tooLarge", "big")),
        (("POST", "/me/messages/draft%2F1/send"), GraphError(500, "boom", "boom")),
    )

    with pytest.raises(GraphError, match="boom"):
        send_mime(graph, message(), [])  # type: ignore[arg-type]

    assert graph.calls[-1] == ("DELETE", "/me/messages/draft%2F1")


def test_any_other_send_failure_is_raised_as_is():
    graph = FakeGraph((("POST", "/me/sendMail"), GraphError(403, "denied", "no")))

    with pytest.raises(GraphError, match="denied"):
        send_mime(graph, message(), [])  # type: ignore[arg-type]
