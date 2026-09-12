"""Reading and changing messages in a Microsoft Graph mailbox."""

from dataclasses import dataclass, field
from datetime import datetime

from ..models.message_header import MessageHeader
from ..threader import NO_SUBJECT
from .graph_session import BatchRequest, GraphError, GraphSession, quote_id, with_query
from .imap_session import FLAG_FLAGGED, FLAG_SEEN

_HEADER_FIELDS = (
    "id,internetMessageId,subject,from,toRecipients,ccRecipients,sentDateTime,"
    "receivedDateTime,isRead,flag,bodyPreview,internetMessageHeaders"
)

# Delta pages default to ten messages; a thousand makes a first pass over a
# large folder a handful of requests instead of hundreds.
_DELTA_PAGE = "odata.maxpagesize=1000"

# The statuses a stale delta link comes back with: Graph has forgotten the
# sync state, and the only way on is to start over.
_EXPIRED_DELTA_STATUSES = frozenset({400, 404, 410})


@dataclass(frozen=True, slots=True)
class DeltaState:
    """Every message id in one folder, and the link that brings it up to date."""

    link: str
    ids: frozenset[str]


@dataclass(slots=True)
class MoveOutcome:
    """Like mail_sync.MoveResult: the ids that made it across, in order, up to
    the first one that did not."""

    destination_ids: list[str | None] = field(default_factory=list)
    failed_index: int | None = None
    error: str | None = None


def _address(recipient: dict | None) -> tuple[str, str]:
    detail = (recipient or {}).get("emailAddress") or {}
    return str(detail.get("name") or ""), str(detail.get("address") or "").lower()


def _header(item: dict, name: str) -> str:
    wanted = name.lower()
    for header in item.get("internetMessageHeaders") or []:
        if str(header.get("name", "")).lower() == wanted:
            return str(header.get("value", ""))
    return ""


def _iso_date(value: str) -> str:
    # Graph's "2026-09-12T10:00:00Z" is already a timestamp; fromisoformat takes
    # the "Z" and yields the offset-carrying form the database stores.
    try:
        return datetime.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        return value


def message_header(item: dict) -> MessageHeader:
    """One message from a Graph listing, in the form a sync stores."""
    sender_name, sender_address = _address(item.get("from"))
    recipients = [_address(r) for r in item.get("toRecipients") or []]
    copied = [_address(r) for r in item.get("ccRecipients") or []]
    recipient_name, recipient_address = next(iter(recipients), ("", ""))
    return MessageHeader(
        uid=str(item["id"]),
        sender=sender_name or sender_address,
        sender_address=sender_address,
        recipient=recipient_name or recipient_address,
        recipient_address=recipient_address,
        subject=str(item.get("subject") or "") or NO_SUBJECT,
        date=_iso_date(
            str(item.get("sentDateTime") or item.get("receivedDateTime") or "")
        ),
        is_unread=not item.get("isRead", False),
        is_starred=(item.get("flag") or {}).get("flagStatus") == "flagged",
        # Graph keeps the body's line breaks and indentation in the preview,
        # which the list row would otherwise show as ragged blank lines.
        preview=" ".join(str(item.get("bodyPreview") or "").split())[:200],
        message_id=str(item.get("internetMessageId") or ""),
        in_reply_to=_header(item, "In-Reply-To"),
        references=_header(item, "References"),
        addresses=[(sender_name, sender_address), *recipients, *copied],
    )


def fetch_headers(
    session: GraphSession, folder_id: str, limit: int, offset: int
) -> list[MessageHeader]:
    """A page of a folder, newest first: offset 0 is the newest `limit`."""
    path = with_query(
        f"/me/mailFolders/{quote_id(folder_id)}/messages",
        top=limit,
        skip=offset,
        orderby="receivedDateTime desc",
        select=_HEADER_FIELDS,
    )
    return [message_header(item) for item in session.get(path).get("value", [])]


def fetch_mime(session: GraphSession, message_id: str) -> bytes:
    """The whole message as RFC 5322 bytes, the same thing an IMAP fetch gives."""
    return session.request("GET", f"/me/messages/{quote_id(message_id)}/$value").body


def folder_ids(
    session: GraphSession, folder_id: str, state: DeltaState | None
) -> DeltaState:
    """Every message id in a folder, brought up to date through a delta query.

    The first call walks the whole folder; later ones pass the previous state
    and only fetch what changed since. A delta link Graph no longer honours
    starts the walk over rather than failing the sync.
    """
    if state is None:
        start = with_query(
            f"/me/mailFolders/{quote_id(folder_id)}/messages/delta", select="id"
        )
        ids: set[str] = set()
    else:
        start, ids = state.link, set(state.ids)

    link = ""
    try:
        for page in session.pages(start, prefer=_DELTA_PAGE):
            for item in page.get("value", []):
                if "@removed" in item:
                    ids.discard(str(item["id"]))
                else:
                    ids.add(str(item["id"]))
            link = page.get("@odata.deltaLink", link)
    except GraphError as error:
        if state is None or error.status not in _EXPIRED_DELTA_STATUSES:
            raise
        return folder_ids(session, folder_id, None)
    return DeltaState(link, frozenset(ids))


def flag_patch(flag: str, should_add: bool) -> dict:
    """The Graph property change for an IMAP flag the window asks for."""
    if flag == FLAG_SEEN:
        return {"isRead": should_add}
    if flag == FLAG_FLAGGED:
        return {"flag": {"flagStatus": "flagged" if should_add else "notFlagged"}}
    raise GraphError(0, "unsupportedFlag", f"no Graph property for {flag}")


def set_flags(
    session: GraphSession, message_ids: list[str], flag: str, should_add: bool
) -> None:
    patch = flag_patch(flag, should_add)
    answers = session.batch(
        [
            BatchRequest("PATCH", f"/me/messages/{quote_id(message_id)}", patch)
            for message_id in message_ids
        ]
    )
    for message_id, answer in zip(message_ids, answers, strict=True):
        if not answer.is_ok:
            raise _batch_error(
                answer.status, answer.body, f"could not flag {message_id}"
            )


def move(
    session: GraphSession, message_ids: list[str], destination_id: str
) -> MoveOutcome:
    """Move messages to another folder.

    The batch runs them all, but the result stops at the first failure, which
    is what the window restores from. One that succeeded after it is put right
    by the next sync of either folder.
    """
    answers = session.batch(
        [
            BatchRequest(
                "POST",
                f"/me/messages/{quote_id(message_id)}/move",
                {"destinationId": destination_id},
            )
            for message_id in message_ids
        ]
    )
    outcome = MoveOutcome()
    for index, answer in enumerate(answers):
        if not answer.is_ok:
            error = _batch_error(answer.status, answer.body, "could not move")
            outcome.failed_index = index
            outcome.error = str(error)
            break
        outcome.destination_ids.append(answer.body.get("id") or None)
    return outcome


def _batch_error(status: int, body: dict, context: str) -> GraphError:
    detail = body.get("error") or {}
    return GraphError(
        status,
        str(detail.get("code", "")),
        f"{context}: {detail.get('message') or f'HTTP {status}'}",
    )
