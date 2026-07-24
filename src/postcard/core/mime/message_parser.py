import email
import email.utils
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.policy import SMTP as email_policy

from ..crypto.types import SignatureEnvelope
from ..models.attachment import Attachment
from .smime import SMIME_SIGNED_PROTOCOLS, extract_signature_envelope


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
    signature_envelope: SignatureEnvelope | None = None


def parse_message(raw: bytes) -> ParsedMessage:
    msg = email.message_from_bytes(raw, policy=email_policy)
    assert isinstance(msg, EmailMessage)

    result = ParsedMessage()
    result.subject = str(msg.get("Subject", ""))
    result.from_display = ", ".join(_addresses(msg, "From"))
    result.to = _addresses(msg, "To")
    result.cc = _addresses(msg, "Cc")
    result.bcc = _addresses(msg, "Bcc")
    result.date = _format_date(msg.get("Date"))

    result.signature_envelope = extract_signature_envelope(msg)

    for part in msg.walk():
        if part.is_multipart():
            continue

        content_type = part.get_content_type()
        disposition = part.get_content_disposition()

        if _is_signature_part(part):
            continue
        elif disposition == "attachment":
            result.attachments.append(_as_attachment(part))
        elif content_type == "text/plain" and result.text_body is None:
            result.text_body = part.get_content()
        elif content_type == "text/html" and result.html_body is None:
            result.html_body = part.get_content()
        else:
            result.attachments.append(_as_attachment(part))

    return result


def _is_signature_part(part: EmailMessage) -> bool:
    ct = part.get_content_type().strip().lower()
    return ct in SMIME_SIGNED_PROTOCOLS


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
