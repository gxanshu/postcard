"""The mail folder tree of a Microsoft Graph mailbox."""

from collections.abc import Iterable
from dataclasses import dataclass

from ..models.folder import FolderRole
from .graph_session import BatchRequest, GraphSession, quote_id, with_query

# Graph's well-known folder names, which resolve to the real folder whatever
# the mailbox language calls it ("Posteingang", "Gesendete Elemente").
WELL_KNOWN_ROLES: dict[str, FolderRole] = {
    "inbox": FolderRole.INBOX,
    "drafts": FolderRole.DRAFTS,
    "sentitems": FolderRole.SENT,
    "archive": FolderRole.ARCHIVE,
    "junkemail": FolderRole.JUNK,
    "deleteditems": FolderRole.TRASH,
}

# Folders Outlook keeps for itself. The server Outbox would sit beside
# Postcard's own, Sync Issues holds conflict copies no other client shows, and
# Conversation History is Teams/Skype chat, not mail.
HIDDEN_WELL_KNOWN = ("outbox", "syncissues", "conversationhistory")

# The sidebar order of the role folders; everything else follows by name.
_ROLE_ORDER = tuple(WELL_KNOWN_ROLES.values())

_FIELDS = (
    "id,displayName,parentFolderId,childFolderCount,unreadItemCount,totalItemCount"
)
_PAGE_SIZE = 250


@dataclass(frozen=True, slots=True)
class GraphFolder:
    id: str
    label: str
    parent_id: str  # "" at the top level
    role: FolderRole
    unread: int
    total: int


def well_known_ids(session: GraphSession) -> dict[str, str]:
    """Map each well-known name this mailbox has to its folder id.

    One $batch for all of them. A mailbox without one (no Archive yet, say)
    answers 404 for it, which just leaves it out.
    """
    names = [*WELL_KNOWN_ROLES, *HIDDEN_WELL_KNOWN]
    answers = session.batch(
        [BatchRequest("GET", f"/me/mailFolders/{name}?$select=id") for name in names]
    )
    return {
        name: str(answer.body["id"])
        for name, answer in zip(names, answers, strict=True)
        if answer.is_ok and "id" in answer.body
    }


def list_folders(
    session: GraphSession, well_known: dict[str, str]
) -> list[GraphFolder]:
    """Every visible folder, parents before their children.

    Graph lists one level per request, so each level's subfolders are fetched
    together in a $batch rather than one request per parent.
    """
    top = _collect(session.pages(_children_path("/me/mailFolders")))
    children: dict[str, list[dict]] = {}
    level = [item for item in top if item.get("childFolderCount")]
    while level:
        answers = session.batch(
            [
                BatchRequest("GET", _children_path(_folder_path(item["id"])))
                for item in level
            ]
        )
        next_level = []
        for parent, answer in zip(level, answers, strict=True):
            if not answer.is_ok:
                continue
            items = list(answer.body.get("value", []))
            next_link = answer.body.get("@odata.nextLink")
            if next_link:
                items.extend(_collect(session.pages(next_link)))
            children[parent["id"]] = items
            next_level.extend(item for item in items if item.get("childFolderCount"))
        level = next_level
    return build_tree(top, children, well_known)


def build_tree(
    top: list[dict], children: dict[str, list[dict]], well_known: dict[str, str]
) -> list[GraphFolder]:
    """Flatten the folder levels depth first, dropping Outlook's own folders
    along with everything under them."""
    roles = {
        well_known[name]: role
        for name, role in WELL_KNOWN_ROLES.items()
        if name in well_known
    }
    hidden = {well_known[name] for name in HIDDEN_WELL_KNOWN if name in well_known}

    def key(item: dict) -> tuple[int, str]:
        role = roles.get(item["id"])
        rank = _ROLE_ORDER.index(role) if role is not None else len(_ROLE_ORDER)
        return rank, str(item.get("displayName", "")).casefold()

    folders: list[GraphFolder] = []

    def visit(items: Iterable[dict], parent_id: str) -> None:
        for item in sorted(items, key=key):
            if item["id"] in hidden:
                continue
            folders.append(
                GraphFolder(
                    id=item["id"],
                    label=str(item.get("displayName", "")),
                    parent_id=parent_id,
                    role=roles.get(item["id"], FolderRole.OTHER),
                    unread=int(item.get("unreadItemCount") or 0),
                    total=int(item.get("totalItemCount") or 0),
                )
            )
            visit(children.get(item["id"], []), item["id"])

    visit(top, "")
    return folders


def _folder_path(folder_id: str) -> str:
    return f"/me/mailFolders/{quote_id(folder_id)}"


def _children_path(parent_path: str) -> str:
    # The root collection is itself the top level's children.
    suffix = "" if parent_path == "/me/mailFolders" else "/childFolders"
    return with_query(parent_path + suffix, top=_PAGE_SIZE, select=_FIELDS)


def _collect(pages: Iterable[dict]) -> list[dict]:
    return [item for page in pages for item in page.get("value", [])]
