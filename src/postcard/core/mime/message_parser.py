import email
import email.utils
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.policy import default as default_policy

from ..models.attachment import Attachment


@dataclass
class ParsedMessage:
    text_body: str | None = None
    html_body: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    subject: str = ""
    from_display: str = ""
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    date: str = ""


def parse_message(raw: bytes) -> ParsedMessage:
    msg = email.message_from_bytes(raw, policy=default_policy)
    assert isinstance(msg, EmailMessage)

    result = ParsedMessage()
    result.subject = str(msg.get("Subject", ""))
    result.from_display = ", ".join(_addresses(msg, "From"))
    result.to = _addresses(msg, "To")
    result.cc = _addresses(msg, "Cc")
    result.bcc = _addresses(msg, "Bcc")
    result.date = _format_date(msg.get("Date"))

    for part in msg.walk():
        if part.is_multipart():
            continue  # a container part -- its children are visited on their own

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
            # if as an attachment rather than silently dropping it
            result.attachments.append(_as_attachment(part))

    return result


def _addresses(msg: EmailMessage, header: str) -> list[str]:
    raw = [str(value) for value in msg.get_all(header, [])]
    out = []
    for name, addr in email.utils.getaddresses(raw):
        if name and addr:
            out.append(f"{name} <{addr}>")
        elif addr:
            out.append(addr)
        elif name:
            out.append(name)
    return out


def _format_date(raw: object) -> str:
    if not raw:
        return ""
    try:
        parsed = email.utils.parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return str(raw)
    return parsed.strftime("%b %d, %Y %H:%M")


def _as_attachment(part: EmailMessage) -> Attachment:
    content = part.get_content()
    if isinstance(content, str):
        content = content.encode("utf-8")
    return Attachment(
        filename=part.get_filename() or "attachment",
        mime_type=part.get_content_type(),
        content=content,
    )


# WebKit's auto-load-images setting only gates <img>; a remote stylesheet,
# @import, @font-face or <iframe> loads regardless and leaks the read just the
# same. Only img-src is toggled: remote CSS is never needed to read mail, so it
# stays blocked even after the user asks for images.
_CSP = "default-src 'none'; style-src 'unsafe-inline'; font-src data:; img-src data:"
_CSP_WITH_IMAGES = _CSP + " https: http:"


def sandbox_html(html: str, *, are_remote_images_allowed: bool) -> str:
    """Wrap a message body in a document whose CSP blocks remote subresources."""
    policy = _CSP_WITH_IMAGES if are_remote_images_allowed else _CSP
    return (
        '<!DOCTYPE html><html><head><meta http-equiv="Content-Security-Policy" '
        f'content="{policy}"></head><body>{html}</body></html>'
    )
