import email
import email.utils
from email.message import EmailMessage
from html import escape
from html.parser import HTMLParser

from .models.attachment import Attachment

_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, _attrs: object) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("p", "blockquote"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = [line.strip() for line in "".join(parser.parts).splitlines()]

    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def _to_html(text: str) -> str:
    return escape(text).replace("\n", "<br>")


def reply_subject(subject: str) -> str:
    if subject.lower().startswith("re:"):
        return subject
    return f"Re: {subject}"


def forward_subject(subject: str) -> str:
    if subject.lower().startswith(("fwd:", "fw:")):
        return subject
    return f"Fwd: {subject}"


def signature_block(text: str) -> str:
    # The "-- " delimiter is the RFC 3676 convention for a signature.
    return f'<div class="signature">-- <br>{_to_html(text)}</div>'


def quote_reply_body(
    original_from: str, original_date: str, original_text: str, signature: str = ""
) -> str:
    return (
        "<div><br></div>"
        + (signature_block(signature) if signature else "")
        + f"<div>On {escape(original_date)}, {escape(original_from)} wrote:</div>"
        + f"<blockquote>{_to_html(original_text)}</blockquote>"
    )


def forward_body(
    original_from: str,
    original_date: str,
    original_subject: str,
    original_text: str,
    signature: str = "",
) -> str:
    return (
        "<div><br></div>"
        + (signature_block(signature) if signature else "")
        + "<div>---------- Forwarded message ----------<br>"
        + f"From: {escape(original_from)}<br>"
        + f"Date: {escape(original_date)}<br>"
        + f"Subject: {escape(original_subject)}</div>"
        + f"<blockquote>{_to_html(original_text)}</blockquote>"
    )


def build_mime_message(
    from_addr: str,
    to_addrs: list[str],
    cc_addrs: list[str],
    subject: str,
    body_html: str,
    attachments: list[Attachment],
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid()
    msg.set_content(html_to_text(body_html))
    msg.add_alternative(body_html, subtype="html")

    for attachment in attachments:
        maintype, _, subtype = attachment.mime_type.partition("/")
        if not subtype:
            # No slash at all ("pdf"): the whole string is unusable as a MIME
            # type, so fall back rather than emitting "pdf/octet-stream".
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(
            attachment.content,
            maintype=maintype or "application",
            subtype=subtype,
            filename=attachment.filename,
        )

    return msg


def extract_recipients(raw: bytes) -> list[str]:
    """Read the To/Cc headers back out of a stored message, for retrying from
    Outbox. Bcc addresses are never written to the stored message, so a Bcc'd
    recipient is lost if the original send failed and is retried later.
    """
    headers = email.message_from_bytes(raw)
    addrs = email.utils.getaddresses(
        [str(headers["To"] or ""), str(headers["Cc"] or "")]
    )
    return [addr for _, addr in addrs if addr]


def suggest_addresses(text: str, addresses: list[str], limit: int = 5) -> list[str]:
    """Known addresses matching the one being typed after the last comma."""
    typed = text.rpartition(",")[2].strip().lower()
    if not typed:
        return []
    return [a for a in addresses if typed in a.lower()][:limit]


def replace_last_address(text: str, address: str) -> str:
    """Swap the address being typed for a picked one, ready for the next."""
    head = text.rpartition(",")[0]
    return f"{head}, {address}, " if head else f"{address}, "
