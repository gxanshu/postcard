import pytest

from postcard.core.net.graph_messages import (
    DeltaState,
    fetch_headers,
    flag_patch,
    folder_ids,
    message_header,
    move,
    set_flags,
)
from postcard.core.net.graph_session import BatchResponse, GraphError
from postcard.core.net.imap_session import FLAG_FLAGGED, FLAG_SEEN
from postcard.core.threader import NO_SUBJECT


def person(name: str, address: str) -> dict:
    return {"emailAddress": {"name": name, "address": address}}


ITEM = {
    "id": "AAMk1",
    "internetMessageId": "<m1@example.com>",
    "subject": "Quarterly numbers",
    "from": person("Ada Lovelace", "Ada@Example.com"),
    "toRecipients": [person("", "grace@example.com"), person("Bob", "bob@example.com")],
    "ccRecipients": [person("Carol", "carol@example.com")],
    "sentDateTime": "2026-09-12T08:30:00Z",
    "receivedDateTime": "2026-09-12T08:31:00Z",
    "isRead": False,
    "flag": {"flagStatus": "flagged"},
    "bodyPreview": "Here they\r\n   are",
    "internetMessageHeaders": [
        {"name": "In-Reply-To", "value": "<m0@example.com>"},
        {"name": "references", "value": "<r@example.com> <m0@example.com>"},
    ],
}


def test_a_graph_message_maps_onto_the_header_a_sync_stores():
    header = message_header(ITEM)

    assert header.uid == "AAMk1"
    assert (header.sender, header.sender_address) == ("Ada Lovelace", "ada@example.com")
    assert (header.recipient, header.recipient_address) == (
        "grace@example.com",
        "grace@example.com",
    )
    assert header.date == "2026-09-12T08:30:00+00:00"
    assert header.is_unread is True
    assert header.is_starred is True
    assert header.preview == "Here they are"
    assert header.message_id == "<m1@example.com>"
    assert header.in_reply_to == "<m0@example.com>"
    assert header.references == "<r@example.com> <m0@example.com>"
    assert ("Carol", "carol@example.com") in header.addresses


def test_a_sparse_message_still_maps():
    header = message_header({"id": "x", "isRead": True})

    assert header.subject == NO_SUBJECT
    assert header.sender == ""
    assert header.is_unread is False
    assert header.is_starred is False
    assert header.date == ""


class FakeGraph:
    def __init__(self, pages=(), batch=(), get=None):
        self._pages = list(pages)
        self._batch = list(batch)
        self._get = get
        self.paths = []
        self.batched = []

    def get(self, path, prefer=""):
        self.paths.append(path)
        return self._get

    def pages(self, path, prefer=""):
        self.paths.append((path, prefer))
        page = self._pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return iter(page)

    def batch(self, requests):
        self.batched.extend(requests)
        return self._batch


def test_fetch_headers_pages_newest_first():
    graph = FakeGraph(get={"value": [ITEM]})

    [header] = fetch_headers(graph, "folder/1", limit=50, offset=100)  # type: ignore[arg-type]

    assert header.uid == "AAMk1"
    path = graph.paths[0]
    assert path.startswith("/me/mailFolders/folder%2F1/messages?$top=50&$skip=100")
    assert "$orderby=receivedDateTime%20desc" in path


def test_a_first_delta_walks_the_folder_and_keeps_the_link():
    graph = FakeGraph(
        pages=[
            [
                {"value": [{"id": "a"}, {"id": "b"}]},
                {"value": [{"id": "c"}], "@odata.deltaLink": "https://delta/1"},
            ]
        ]
    )

    state = folder_ids(graph, "inbox", None)  # type: ignore[arg-type]

    assert state == DeltaState("https://delta/1", frozenset({"a", "b", "c"}))
    assert graph.paths[0][1] == "odata.maxpagesize=1000"


def test_a_later_delta_applies_additions_and_removals():
    graph = FakeGraph(
        pages=[
            [
                {
                    "value": [
                        {"id": "d"},
                        {"id": "a", "@removed": {"reason": "deleted"}},
                    ],
                    "@odata.deltaLink": "https://delta/2",
                }
            ]
        ]
    )

    state = folder_ids(graph, "inbox", DeltaState("https://delta/1", frozenset("abc")))  # type: ignore[arg-type]

    assert state == DeltaState("https://delta/2", frozenset({"b", "c", "d"}))
    assert graph.paths[0][0] == "https://delta/1"


def test_an_expired_delta_link_starts_over_instead_of_failing():
    graph = FakeGraph(
        pages=[
            GraphError(410, "SyncStateNotFound", "gone"),
            [{"value": [{"id": "z"}], "@odata.deltaLink": "https://delta/new"}],
        ]
    )

    state = folder_ids(graph, "inbox", DeltaState("https://delta/old", frozenset("ab")))  # type: ignore[arg-type]

    assert state == DeltaState("https://delta/new", frozenset({"z"}))


def test_a_first_delta_that_fails_is_raised():
    graph = FakeGraph(pages=[GraphError(410, "gone", "gone")])

    with pytest.raises(GraphError):
        folder_ids(graph, "inbox", None)  # type: ignore[arg-type]


def test_imap_flags_become_graph_properties():
    assert flag_patch(FLAG_SEEN, True) == {"isRead": True}
    assert flag_patch(FLAG_FLAGGED, False) == {"flag": {"flagStatus": "notFlagged"}}
    with pytest.raises(GraphError):
        flag_patch("\\Answered", True)


def test_set_flags_patches_every_message_and_raises_on_a_refusal():
    graph = FakeGraph(
        batch=[
            BatchResponse(200, {}),
            BatchResponse(403, {"error": {"code": "denied", "message": "nope"}}),
        ]
    )

    with pytest.raises(GraphError, match="could not flag b: nope"):
        set_flags(graph, ["a", "b"], FLAG_SEEN, True)  # type: ignore[arg-type]

    assert [(r.method, r.path, r.body) for r in graph.batched] == [
        ("PATCH", "/me/messages/a", {"isRead": True}),
        ("PATCH", "/me/messages/b", {"isRead": True}),
    ]


def test_move_reports_the_ids_up_to_the_first_failure():
    graph = FakeGraph(
        batch=[
            BatchResponse(201, {"id": "a"}),
            BatchResponse(404, {"error": {"message": "not found"}}),
            BatchResponse(201, {"id": "c"}),
        ]
    )

    outcome = move(graph, ["a", "b", "c"], "archive-id")  # type: ignore[arg-type]

    assert outcome.destination_ids == ["a"]
    assert outcome.failed_index == 1
    assert outcome.error is not None and "not found" in outcome.error
    assert graph.batched[0].body == {"destinationId": "archive-id"}


def test_a_move_that_all_succeeds_has_no_failure():
    graph = FakeGraph(batch=[BatchResponse(201, {"id": "a"})])

    outcome = move(graph, ["a"], "dest")  # type: ignore[arg-type]

    assert (outcome.destination_ids, outcome.failed_index) == (["a"], None)
