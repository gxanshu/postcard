import email
import email.utils
from dataclasses import dataclass
from email.message import EmailMessage, Message
from html import escape
from html.parser import HTMLParser
from urllib.parse import unquote

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


def to_html(text: str) -> str:
    return escape(text).replace("\n", "<br>")


def reply_subject(subject: str) -> str:
    if subject.lower().startswith("re:"):
        return subject
    return f"Re: {subject}"


# Reply All keeps the rest of the thread in the loop: the original To and Cc,
# minus ourselves and minus whoever the reply is already addressed to.
def reply_all_cc(headers: Message, own_email: str, to_addr: str) -> str:
    excluded = {own_email.lower(), to_addr.lower()}
    recipients = email.utils.getaddresses(
        [str(headers.get("To", "")), str(headers.get("Cc", ""))]
    )
    # dict keeps the header's own order; a set would shuffle the recipients.
    unique_addrs = dict.fromkeys(
        addr for _name, addr in recipients if addr and addr.lower() not in excluded
    )
    return ", ".join(unique_addrs)


def forward_subject(subject: str) -> str:
    if subject.lower().startswith(("fwd:", "fw:")):
        return subject
    return f"Fwd: {subject}"


def signature_block(text: str) -> str:
    # The "-- " delimiter is the RFC 3676 convention for a signature.
    return f'<div class="signature">-- <br>{to_html(text)}</div>'


def quote_reply_body(
    original_from: str, original_date: str, original_text: str, signature: str = ""
) -> str:
    return (
        "<div><br></div>"
        + (signature_block(signature) if signature else "")
        + f"<div>On {escape(original_date)}, {escape(original_from)} wrote:</div>"
        + f"<blockquote>{to_html(original_text)}</blockquote>"
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
        + f"<blockquote>{to_html(original_text)}</blockquote>"
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


@dataclass(frozen=True)
class MailtoDraft:
    """The composer fields a mailto: link asks for."""

    to: str = ""
    cc: str = ""
    bcc: str = ""
    subject: str = ""
    body_html: str = ""


def _mailto_headers(query: str) -> dict[str, str]:
    # Not urllib.parse.parse_qsl: it decodes "+" as a space, which would break
    # plus-addressed recipients. RFC 6068 encodes spaces as %20 only.
    headers: dict[str, str] = {}
    for part in query.split("&"):
        key, _, value = part.partition("=")
        if key:
            headers.setdefault(unquote(key).lower(), unquote(value))
    return headers


def parse_mailto(uri: str) -> MailtoDraft:
    """Split an RFC 6068 mailto: URI into composer fields.

    The body is escaped into HTML here: it arrives from outside the app, and
    the composer renders its body as HTML.
    """
    path, _, query = uri.partition("?")
    headers = _mailto_headers(query)
    addressed = (unquote(path.partition(":")[2]), headers.get("to", ""))
    recipients = [addr for addr in addressed if addr]
    body = headers.get("body", "")
    return MailtoDraft(
        to=", ".join(recipients),
        cc=headers.get("cc", ""),
        bcc=headers.get("bcc", ""),
        subject=headers.get("subject", ""),
        body_html=to_html(body) if body else "",
    )


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
