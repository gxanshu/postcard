import socket
import ssl
from gettext import gettext as _

from .imap_session import ImapError
from .smtp_session import SmtpError

# Substrings (lowercased) that mean the server rejected the credentials.
_AUTH_HINTS = (
    "authenticationfailed",
    "authentication failed",
    "invalid credentials",
    "username and password not accepted",
    "login failed",
    "5.7.8",
)


# Turn a raw exception into a (category, friendly message) pair. Categories:
# "auth", "unreachable", "tls", "server". Order matters — most socket errors
# subclass OSError, so the specific cases are checked first.
def classify(exc: Exception, host: str) -> tuple[str, str]:
    if isinstance(exc, ssl.SSLError):
        return "tls", _("Couldn't establish a secure connection to {host}.").format(
            host=host
        )
    if isinstance(exc, socket.gaierror):
        return "unreachable", _(
            "Can't find {host}. Check the server address or your connection."
        ).format(host=host)
    if isinstance(exc, ConnectionRefusedError):
        return "unreachable", _(
            "{host} refused the connection. Check the port."
        ).format(host=host)
    if isinstance(exc, TimeoutError):
        return "unreachable", _("Connecting to {host} timed out.").format(host=host)
    if isinstance(exc, (ImapError, SmtpError)):
        text = str(exc).lower()
        if any(hint in text for hint in _AUTH_HINTS):
            return "auth", _("Sign-in failed. Check the account password.")
        return "server", str(exc)
    if isinstance(exc, OSError):
        return "unreachable", _(
            "Couldn't reach the mail server. Check your connection."
        )
    return "server", str(exc)
