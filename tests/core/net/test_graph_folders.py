from postcard.core.models.folder import FolderRole
from postcard.core.net.graph_folders import build_tree, list_folders, well_known_ids
from postcard.core.net.graph_session import BatchResponse

WELL_KNOWN = {
    "inbox": "in",
    "sentitems": "sent",
    "deleteditems": "bin",
    "outbox": "out",
    "syncissues": "sync",
}


def item(folder_id: str, label: str, children: int = 0, unread: int = 0) -> dict:
    return {
        "id": folder_id,
        "displayName": label,
        "childFolderCount": children,
        "unreadItemCount": unread,
        "totalItemCount": unread + 1,
    }


def test_role_folders_come_first_in_their_own_order_then_the_rest_by_name():
    top = [
        item("zeta", "Zeta"),
        item("bin", "Gelöschte Elemente"),
        item("alpha", "alpha"),
        item("sent", "Gesendete Elemente"),
        item("in", "Posteingang"),
    ]

    folders = build_tree(top, {}, WELL_KNOWN)

    assert [f.label for f in folders] == [
        "Posteingang",
        "Gesendete Elemente",
        "Gelöschte Elemente",
        "alpha",
        "Zeta",
    ]
    assert [f.role for f in folders[:3]] == [
        FolderRole.INBOX,
        FolderRole.SENT,
        FolderRole.TRASH,
    ]
    assert folders[3].role is FolderRole.OTHER


def test_a_folder_named_like_a_role_is_not_given_it():
    # Outlook mailboxes can hold both the real "Archiv" and a stray "Archive";
    # only the id Graph names as the archive gets the role.
    folders = build_tree(
        [item("real", "Archiv"), item("stray", "Archive")],
        {},
        {"archive": "real"},
    )

    assert {f.label: f.role for f in folders} == {
        "Archiv": FolderRole.ARCHIVE,
        "Archive": FolderRole.OTHER,
    }


def test_children_follow_their_parent_and_name_it():
    top = [item("in", "Posteingang", children=1), item("other", "Other")]
    children = {"in": [item("kid", "Projekte", children=1)], "kid": [item("gk", "Q3")]}

    folders = build_tree(top, children, WELL_KNOWN)

    assert [(f.id, f.parent_id) for f in folders] == [
        ("in", ""),
        ("kid", "in"),
        ("gk", "kid"),
        ("other", ""),
    ]


def test_outlook_s_own_folders_are_hidden_with_everything_under_them():
    top = [item("out", "Postausgang"), item("sync", "Sync Issues", children=1)]
    children = {"sync": [item("conflicts", "Conflicts")]}

    assert build_tree(top, children, WELL_KNOWN) == []


def test_counts_are_carried_through():
    [folder] = build_tree([item("in", "Inbox", unread=4)], {}, WELL_KNOWN)

    assert (folder.unread, folder.total) == (4, 5)


class FakeGraph:
    def __init__(self, pages=(), batches=()):
        self._pages = list(pages)
        self._batches = list(batches)
        self.batched = []

    def pages(self, path, prefer=""):
        return iter(self._pages.pop(0))

    def batch(self, requests):
        self.batched.append([request.path for request in requests])
        return self._batches.pop(0)


def test_well_known_ids_leaves_out_what_the_mailbox_does_not_have():
    graph = FakeGraph(
        batches=[
            [BatchResponse(200, {"id": "in"})] + [BatchResponse(404, {"error": {}})] * 8
        ]
    )

    assert well_known_ids(graph) == {"inbox": "in"}  # type: ignore[arg-type]
    assert graph.batched[0][0] == "/me/mailFolders/inbox?$select=id"


def test_list_folders_fetches_each_level_of_subfolders_in_one_batch():
    graph = FakeGraph(
        pages=[
            [{"value": [item("a", "A", children=1), item("b", "B", children=1)]}],
            [{"value": [item("a3", "A3")]}],
        ],
        batches=[
            [
                BatchResponse(
                    200,
                    {
                        "value": [item("a1", "A1", children=1)],
                        "@odata.nextLink": "https://next",
                    },
                ),
                BatchResponse(200, {"value": [item("b1", "B1")]}),
            ],
            [BatchResponse(200, {"value": [item("a2", "A2")]})],
        ],
    )

    folders = list_folders(graph, {})  # type: ignore[arg-type]

    assert [f.id for f in folders] == ["a", "a1", "a2", "a3", "b", "b1"]
    assert len(graph.batched) == 2
    assert graph.batched[0][0].startswith("/me/mailFolders/a/childFolders?$top=250")
