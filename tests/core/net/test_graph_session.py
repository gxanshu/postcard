import json

import pytest

from postcard.core.net.auth import Credential
from postcard.core.net.graph_session import (
    BATCH_LIMIT,
    GRAPH_ROOT,
    UPLOAD_CHUNK_BYTES,
    BatchRequest,
    GraphError,
    GraphSession,
    Response,
    quote_id,
    retry_delay,
    with_query,
)

TOKEN = Credential("ada@example.com", "token-123", "xoauth2")


def reply(status: int = 200, body: dict | None = None, **headers: str) -> Response:
    return Response(
        status, json.dumps(body).encode() if body is not None else b"", headers
    )


class FakeTransport:
    """Answers each request with the next queued Response and records it."""

    def __init__(self, *responses: Response) -> None:
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


def session(
    transport: FakeTransport, sleeps: list[float] | None = None
) -> GraphSession:
    return GraphSession(
        TOKEN,
        transport,
        sleep=(sleeps.append if sleeps is not None else lambda _s: None),
    )


def test_a_password_cannot_open_a_graph_session():
    with pytest.raises(GraphError, match="login cannot sign in"):
        GraphSession(Credential("ada@example.com", "hunter2"))


def test_requests_carry_the_token_and_ask_for_immutable_ids():
    transport = FakeTransport(reply(body={"value": []}))

    session(transport).get("/me/mailFolders")

    request = transport.requests[0]
    assert request.full_url == GRAPH_ROOT + "/me/mailFolders"
    assert request.get_header("Authorization") == "Bearer token-123"
    assert request.get_header("Prefer") == 'IdType="ImmutableId"'


def test_an_extra_preference_joins_the_immutable_id_one():
    transport = FakeTransport(reply(body={}))

    session(transport).get("/x", prefer="odata.maxpagesize=1000")

    assert transport.requests[0].get_header("Prefer") == (
        'IdType="ImmutableId", odata.maxpagesize=1000'
    )


def test_throttling_is_retried_after_the_delay_graph_asks_for():
    sleeps: list[float] = []
    transport = FakeTransport(
        reply(429, {"error": {"code": "TooManyRequests"}}, **{"Retry-After": "7"}),
        reply(body={"ok": True}),
    )

    assert session(transport, sleeps).get("/me") == {"ok": True}
    assert sleeps == [7.0]


def test_a_backend_that_stays_busy_raises_after_the_last_attempt():
    transport = FakeTransport(*[reply(503, {"error": {"code": "busy"}})] * 4)

    with pytest.raises(GraphError) as raised:
        session(transport).get("/me")

    assert raised.value.status == 503
    assert len(transport.requests) == 4


def test_an_error_status_carries_graph_s_code_and_message():
    transport = FakeTransport(
        reply(404, {"error": {"code": "ErrorItemNotFound", "message": "gone"}})
    )

    with pytest.raises(GraphError) as raised:
        session(transport).get("/me/messages/x")

    assert (raised.value.status, raised.value.code, raised.value.message) == (
        404,
        "ErrorItemNotFound",
        "gone",
    )


def test_an_error_without_a_json_body_still_names_the_status():
    transport = FakeTransport(Response(502, b"<html>bad gateway</html>"))

    with pytest.raises(GraphError, match="HTTP 502"):
        session(transport).get("/me")


def test_retry_delay_is_capped_and_falls_back_to_backoff():
    assert retry_delay({"retry-after": "3600"}, 0) == 30
    assert retry_delay({}, 2) == 4.0


def test_pages_follows_next_links_to_the_end():
    transport = FakeTransport(
        reply(body={"value": [1], "@odata.nextLink": "https://graph.microsoft.com/p2"}),
        reply(body={"value": [2]}),
    )

    pages = list(session(transport).pages("/me/mailFolders"))

    assert [page["value"] for page in pages] == [[1], [2]]
    assert transport.requests[1].full_url == "https://graph.microsoft.com/p2"


def batch_reply(*answers: tuple[str, int, dict]) -> Response:
    return reply(
        body={
            "responses": [
                {"id": index, "status": status, "body": body}
                for index, status, body in answers
            ]
        }
    )


def test_batch_answers_come_back_in_request_order_whatever_order_graph_used():
    transport = FakeTransport(batch_reply(("1", 200, {"n": 1}), ("0", 201, {"n": 0})))

    answers = session(transport).batch(
        [BatchRequest("GET", "/a"), BatchRequest("POST", "/b", {"x": 1})]
    )

    assert [(a.status, a.body["n"]) for a in answers] == [(201, 0), (200, 1)]
    sent = json.loads(transport.requests[0].data)
    assert sent["requests"][1] == {
        "id": "1",
        "method": "POST",
        "url": "/b",
        "headers": {
            "Prefer": 'IdType="ImmutableId"',
            "Content-Type": "application/json",
        },
        "body": {"x": 1},
    }


def test_batch_splits_at_graph_s_limit():
    count = BATCH_LIMIT + 3
    transport = FakeTransport(
        batch_reply(*[(str(i), 200, {}) for i in range(BATCH_LIMIT)]),
        batch_reply(*[(str(i), 200, {}) for i in range(BATCH_LIMIT, count)]),
    )

    answers = session(transport).batch([BatchRequest("GET", "/a")] * count)

    assert len(answers) == count
    assert [len(json.loads(r.data)["requests"]) for r in transport.requests] == [
        BATCH_LIMIT,
        3,
    ]


def test_a_request_throttled_inside_a_batch_is_retried_alone():
    transport = FakeTransport(
        batch_reply(("0", 200, {}), ("1", 429, {})),
        batch_reply(("1", 200, {"retried": True})),
    )

    answers = session(transport).batch([BatchRequest("GET", "/a")] * 2)

    assert answers[1].body == {"retried": True}
    assert [r["id"] for r in json.loads(transport.requests[1].data)["requests"]] == [
        "1"
    ]


def test_a_request_graph_leaves_unanswered_is_reported_as_failed():
    transport = FakeTransport(batch_reply(("0", 200, {})))

    answers = session(transport).batch([BatchRequest("GET", "/a")] * 2)

    assert answers[1].is_ok is False


def test_upload_sends_ranged_chunks_without_the_bearer_token():
    transport = FakeTransport(Response(200), Response(201))
    data = b"x" * (UPLOAD_CHUNK_BYTES + 10)

    session(transport).upload("https://outlook.office.com/upload?sig=1", data)

    first, second = transport.requests
    assert first.get_header("Authorization") is None
    assert (
        first.get_header("Content-range")
        == f"bytes 0-{UPLOAD_CHUNK_BYTES - 1}/{len(data)}"
    )
    assert second.get_header("Content-range") == (
        f"bytes {UPLOAD_CHUNK_BYTES}-{len(data) - 1}/{len(data)}"
    )


def test_ids_and_query_options_are_url_safe():
    assert quote_id("AAMk+a/b=") == "AAMk%2Ba%2Fb%3D"
    assert with_query(
        "/m", top=50, orderby="receivedDateTime desc", select="id,subject"
    ) == ("/m?$top=50&$orderby=receivedDateTime%20desc&$select=id,subject")
