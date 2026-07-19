import email
import email.utils
from email.message import EmailMessage

from .models.attachment import Attachment


def reply_subject(subject: str) -> str:
    if subject.lower().startswith("re:"):
        return subject
    return f"Re: {subject}"


def forward_subject(subject: str) -> str:
    if subject.lower().startswith("fwd:"):
        return subject
    return f"Fwd: {subject}"


def quote_reply_body(original_from: str, original_date: str, original_text: str) -> str:
    quoted = "\n".join(f"> {line}" for line in original_text.splitlines())
    return f"\n\nOn {original_date}, {original_from} wrote:\n{quoted}"


def forward_body(
    original_from: str, original_date: str, original_subject: str, original_text: str
) -> str:
    return (
        "\n\n---------- Forwarded message ----------\n"
        f"From: {original_from}\n"
        f"Date: {original_date}\n"
        f"Subject: {original_subject}\n\n"
        f"{original_text}"
    )


def build_mime_message(
    from_addr: str,
    to_addrs: list[str],
    subject: str,
    body_text: str,
    attachments: list[Attachment],
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid()
    msg.set_content(body_text)

    for attachment in attachments:
        maintype, _, subtype = attachment.mime_type.partition("/")
        msg.add_attachment(
            attachment.content,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=attachment.filename,
        )

    return msg


def extract_to_addresses(raw: bytes) -> list[str]:
    """Read the To header back out of a stored message, for retrying from Outbox."""
    headers = email.message_from_bytes(raw)
    to_header = str(headers["To"] or "")
    return [addr for _, addr in email.utils.getaddresses([to_header]) if addr]
