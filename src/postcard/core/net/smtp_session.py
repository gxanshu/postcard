import smtplib


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
            self._smtp = smtplib.SMTP(self._host, self._port, timeout=30)
            self._smtp.starttls()
        else:
            self._smtp = smtplib.SMTP_SSL(self._host, self._port, timeout=30)

    def login(self, user: str, password: str) -> None:
        try:
            if self._smtp is not None:
                self._smtp.login(user, password)
        except smtplib.SMTPException as error:
            raise SmtpError(str(error)) from error

    def send_raw(self, from_addr: str, recipients: list[str], raw: bytes) -> None:
        try:
            if self._smtp is not None:
                self._smtp.sendmail(from_addr, recipients, raw)
        except smtplib.SMTPException as error:
            raise SmtpError(str(error)) from error

    def quit(self) -> None:
        try:
            if self._smtp is not None:
                self._smtp.quit()
        except Exception:
            pass
