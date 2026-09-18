"""Sending a composed message through Microsoft Graph.

Graph takes the finished MIME message as it is, so what the composer built --
headers, Message-ID, HTML and attachments -- goes out unchanged, and Exchange
files the copy in Sent Items itself.
"""

import base64
import email
import logging
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.utils import getaddresses

from .graph_session import GraphError, GraphSession, quote_id

logger = logging.getLogger(__name__)

MIME_TYPE = "text/plain"  # what Graph wants base64 MIME labelled as

# Graph refuses a request body past its size limit with 413.
_TOO_LARGE = 413

# Attachments under this size are posted inline; larger ones need an upload
# session. Graph documents the inline limit as "under 3 MB" and the upload
# session as 3 MB to 150 MB, so the cutoff is the decimal 3 MB rather than
# 3 MiB -- an attachment between the two is over Graph's limit but would have
# taken the inline path and failed the whole send. It doubles as the bound that
# keeps the request body legal: contentBytes is base64, which is a third larger
# again, and 3 MB of attachment encodes to 4 MB of JSON.
_INLINE_ATTACHMENT_BYTES = 3 * 1000 * 1000


@dataclass(frozen=True, slots=True)
class DetachedAttachment:
    filename: str
    content_type: str
    content: bytes


def send_mime(session: GraphSession, raw: bytes, recipients: list[str]) -> None:
    """Send raw to every recipient.

    Graph delivers to the addresses in the message's own headers, not to an
    envelope, so a Bcc recipient -- one in `recipients` but not in To or Cc --
    is written into a Bcc header for Graph to act on and strip. A message too
    large for one request is rebuilt as a draft with its attachments uploaded
    separately.
    """
    outgoing = with_bcc(raw, bcc_recipients(raw, recipients))
    try:
        session.request(
            "POST", "/me/sendMail", base64.b64encode(outgoing), content_type=MIME_TYPE
        )
    except GraphError as error:
        if error.status != _TOO_LARGE:
            raise
        logger.debug("message too large for sendMail; uploading attachments")
        _send_as_draft(session, outgoing)


def bcc_recipients(raw: bytes, recipients: list[str]) -> list[str]:
    """The recipients the message's To and Cc headers don't name.

    A recipient arrives in whatever form the composer was given -- a bare
    address or "Alice <alice@example.com>" -- while the headers name the
    address alone, so both sides are parsed down to the address before they are
    compared. Matching the raw strings instead would read a named To recipient
    as hidden: they would be sent a second copy, with their address exposed in
    the Bcc header of everyone else's.
    """
    headers = email.message_from_bytes(raw, policy=policy.compat32)
    named = _addresses(str(headers["To"] or ""), str(headers["Cc"] or ""))
    hidden = []
    seen: set[str] = set()
    for recipient in recipients:
        address = next(iter(_addresses(recipient)), recipient.casefold())
        if address not in named and address not in seen:
            seen.add(address)
            hidden.append(recipient)
    return hidden


def _addresses(*values: str) -> list[str]:
    """Every address the given header values name, lowercased."""
    return [
        address.casefold() for _name, address in getaddresses(list(values)) if address
    ]


def with_bcc(raw: bytes, bcc: list[str]) -> bytes:
    """raw with a Bcc header in front, leaving every other byte as it was."""
    if not bcc:
        return raw
    line_end = b"\r\n" if raw.split(b"\n", 1)[0].endswith(b"\r") else b"\n"
    return b"Bcc: " + ", ".join(bcc).encode() + line_end + raw


def split_attachments(raw: bytes) -> tuple[bytes, list[DetachedAttachment]]:
    """raw without its attachments, and the attachments it had."""
    message = email.message_from_bytes(raw, policy=policy.default)
    if not isinstance(message, EmailMessage) or not message.is_multipart():
        return raw, []

    parts = list(message.iter_attachments())
    detached = [
        DetachedAttachment(
            part.get_filename() or "attachment",
            part.get_content_type(),
            _decoded(part),
        )
        for part in parts
    ]
    kept = [
        part
        for part in message.get_payload()
        if not any(part is attachment for attachment in parts)
    ]
    message.set_payload(kept)
    return message.as_bytes(), detached


def _decoded(part: EmailMessage) -> bytes:
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else b""


def _send_as_draft(session: GraphSession, raw: bytes) -> None:
    body, attachments = split_attachments(raw)
    draft = session.request(
        "POST", "/me/messages", base64.b64encode(body), content_type=MIME_TYPE
    ).json()
    draft_path = f"/me/messages/{quote_id(draft['id'])}"
    try:
        for attachment in attachments:
            _attach(session, draft_path, attachment)
        session.request("POST", f"{draft_path}/send", b"")
    except Exception:
        # Left behind, the half-built draft would sit in Drafts looking like
        # something the user saved.
        try:
            session.request("DELETE", draft_path)
        except (GraphError, OSError):
            logger.warning("could not remove an unsent draft", exc_info=True)
        raise


def _attach(
    session: GraphSession, draft_path: str, attachment: DetachedAttachment
) -> None:
    if len(attachment.content) < _INLINE_ATTACHMENT_BYTES:
        session.send(
            "POST",
            f"{draft_path}/attachments",
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": attachment.filename,
                "contentType": attachment.content_type,
                "contentBytes": base64.b64encode(attachment.content).decode(),
            },
        )
        return
    upload = session.send(
        "POST",
        f"{draft_path}/attachments/createUploadSession",
        {
            "AttachmentItem": {
                "attachmentType": "file",
                "name": attachment.filename,
                "size": len(attachment.content),
                "contentType": attachment.content_type,
            }
        },
    )
    session.upload(str(upload["uploadUrl"]), attachment.content)
