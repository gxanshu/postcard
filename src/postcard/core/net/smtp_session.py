import logging
import smtplib

from . import NET_TIMEOUT_SECONDS, ssl_context_for
from .auth import MECHANISM_LOGIN, MECHANISM_XOAUTH2, Credential, xoauth2_response

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
        context = ssl_context_for(self._host)
        if self._security == "starttls":
            self._smtp = smtplib.SMTP(
                self._host, self._port, timeout=NET_TIMEOUT_SECONDS
            )
            self._smtp.starttls(context=context)
        else:
            self._smtp = smtplib.SMTP_SSL(
                self._host, self._port, timeout=NET_TIMEOUT_SECONDS, context=context
            )

    def sign_in(self, credential: Credential) -> None:
        smtp = self._require_smtp()
        try:
            if credential.mechanism == MECHANISM_XOAUTH2:
                # login() greets the server on our behalf; auth() does not, and
                # SMTP_SSL has not greeted it either by this point.
                smtp.ehlo_or_helo_if_needed()
                smtp.auth(
                    "XOAUTH2",
                    lambda _challenge=None: xoauth2_response(
                        credential.user, credential.secret
                    ),
                    initial_response_ok=True,
                )
            elif credential.mechanism == MECHANISM_LOGIN:
                smtp.login(credential.user, credential.secret)
            else:
                raise SmtpError(f"unsupported mechanism {credential.mechanism}")
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
