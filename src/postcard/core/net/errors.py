import html
import re
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


# Turn a raw exception into an (is it the password?, friendly message) pair.
# Only that one distinction is acted on -- a rejected password is the failure
# no Retry button can fix. Order matters: most socket errors subclass OSError,
# so the specific cases are checked first.
def classify(exc: Exception, host: str) -> tuple[bool, str]:  # noqa: PLR0911
    # noqa PLR0911: a dispatch table. One return per exception category is the
    # clearest shape here; collapsing them into a dict costs readability.
    if isinstance(exc, ssl.SSLError):
        return False, _("Couldn't establish a secure connection to {host}.").format(
            host=host
        )
    if isinstance(exc, socket.gaierror):
        return False, _(
            "Can't find {host}. Check the server address or your connection."
        ).format(host=host)
    if isinstance(exc, ConnectionRefusedError):
        return False, _("{host} refused the connection. Check the port.").format(
            host=host
        )
    if isinstance(exc, TimeoutError):
        return False, _("Connecting to {host} timed out.").format(host=host)
    if isinstance(exc, (ImapError, SmtpError)):
        text = str(exc).lower()
        if any(hint in text for hint in _AUTH_HINTS):
            return True, _("Sign-in failed. Check the account password.")
        return False, str(exc)
    if isinstance(exc, OSError):
        return False, _("Couldn't reach the mail server. Check your connection.")
    return False, str(exc)


# Trailing punctuation is sentence, not URL: "see https://x/y." keeps the dot out.
_URL_PATTERN = re.compile(r"https?://[^\s<>]*[^\s<>.,;:)\]]")


# Server messages often carry a help URL ("Application-specific password
# required: https://support.google.com/..."). The banner renders Pango markup,
# so escape the text first, then turn bare URLs into clickable links. Quotes are
# escaped too: an unescaped one would break out of the href="..." attribute.
def linkify(text: str) -> str:
    escaped = html.escape(text)
    return _URL_PATTERN.sub(lambda m: f'<a href="{m.group()}">{m.group()}</a>', escaped)
