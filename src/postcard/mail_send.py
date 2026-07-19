from .core.models.account import Account
from .core.net.smtp_session import SmtpSession


def send_message(
    account: Account, password: str, from_addr: str, recipients: list[str], raw: bytes
) -> None:
    """Connect, log in, and hand a fully-built message to the server."""
    session = SmtpSession(account.smtp_host, account.smtp_port, account.smtp_security)
    session.connect()

    try:
        session.login(account.email, password)
        session.send_raw(from_addr, recipients, raw)
    finally:
        session.quit()
