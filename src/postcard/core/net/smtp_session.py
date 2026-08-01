import logging
import smtplib

from . import NET_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


class SmtpError(Exception):
    """Raised when talking to the server fails (bad login, dropped link, ..)"""


class SmtpSession:
    def __init__(self, host: str, port: int, security: str = "tls") -> None:
        self._host = host
        self._port = port
        self._security = security
        self._smtp: smtplib.SMTP | None = None

    def connect(self) -> None:
        if self._security == "starttls":
            self._smtp = smtplib.SMTP(
                self._host, self._port, timeout=NET_TIMEOUT_SECONDS
            )
            self._smtp.starttls()
        else:
            self._smtp = smtplib.SMTP_SSL(
                self._host, self._port, timeout=NET_TIMEOUT_SECONDS
            )

    def login(self, user: str, password: str) -> None:
        try:
            self._require_smtp().login(user, password)
        except smtplib.SMTPException as error:
            raise SmtpError(str(error)) from error

    def send_raw(self, from_addr: str, recipients: list[str], raw: bytes) -> None:
        try:
            self._require_smtp().sendmail(from_addr, recipients, raw)
        except smtplib.SMTPException as error:
            raise SmtpError(
                f"could not send to {', '.join(recipients)}: {error}"
            ) from error

    def _require_smtp(self) -> smtplib.SMTP:
        # Never return None: `if self._smtp is not None` around the sendmail
        # call meant a session that never connected returned normally, and
        # mail_sync.send_message reported the mail as sent.
        if self._smtp is None:
            raise SmtpError(f"not connected to {self._host}:{self._port}")
        return self._smtp

    def quit(self) -> None:
        # Same contract as ImapSession.logout: called from a `finally:`, so it
        # must never raise over the top of the real error.
        try:
            if self._smtp is not None:
                self._smtp.quit()
        except Exception:
            logger.debug("SMTP quit to %s failed", self._host, exc_info=True)
