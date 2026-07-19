import email
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.policy import default as default_policy

from postbox.core.models import attachment

from ..models.attachment import Attachment


@dataclass
class ParsedMessage:
    text_body: str | None = None
    html_body: str | None = None
    attachments: list[Attachment] = field(default_factory=list)


def parse_message(raw: bytes) -> ParsedMessage:
    msg = email.message_from_bytes(raw, policy=default_policy)
    assert isinstance(msg, EmailMessage)

    result = ParsedMessage()
    for part in msg.walk():
        if part.is_multipart():
            continue  # a contianer part -- its children are visited on their own

        content_type = part.get_content_type()
        disposition = part.get_content_disposition()

        if disposition == "attachment":
            result.attachments.append(_as_attachment(part))
        elif content_type == "text/plain" and result.text_body is None:
            result.text_body = part.get_content()
        elif content_type == "text/html" and result.html_body is None:
            result.html_body = part.get_content()
        else:
            # anything else (an inline image, unrecognised type) -- treat
            # if as an attachment rather then silently dropping it
            result.attachments.append(_as_attachment(part))

    return result


def _as_attachment(part: EmailMessage) -> Attachment:
    content = part.get_content()
    if isinstance(content, str):
        content = content.encode("utf-8")
    return Attachment(
        filename=part.get_filename() or "attachment",
        mime_type=part.get_content_type(),
        content=content,
    )
