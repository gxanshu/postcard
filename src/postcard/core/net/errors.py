import html
import re
import socket
import ssl
from gettext import gettext as _

from .imap_session import ImapError
from .smtp_session import SmtpError

# The categories classify() can return. Callers branch on these, so they are
# named rather than spelled out at each comparison.
CATEGORY_AUTH = "auth"
CATEGORY_UNREACHABLE = "unreachable"
CATEGORY_TLS = "tls"
CATEGORY_SERVER = "server"

# Substrings (lowercased) that mean the server rejected the credentials.
_AUTH_HINTS = (
    "authenticationfailed",
    "authentication failed",
    "invalid credentials",
    "username and password not accepted",
    "login failed",
    "5.7.8",
)


# Turn a raw exception into a (category, friendly message) pair. Order matters
# — most socket errors subclass OSError, so specific cases are checked first.
def classify(exc: Exception, host: str) -> tuple[str, str]:  # noqa: PLR0911
    # noqa PLR0911: a dispatch table. One return per exception category is the
    # clearest shape here; collapsing them into a dict costs readability.
    if isinstance(exc, ssl.SSLError):
        return CATEGORY_TLS, _(
            "Couldn't establish a secure connection to {host}."
        ).format(host=host)
    if isinstance(exc, socket.gaierror):
        return CATEGORY_UNREACHABLE, _(
            "Can't find {host}. Check the server address or your connection."
        ).format(host=host)
    if isinstance(exc, ConnectionRefusedError):
        return CATEGORY_UNREACHABLE, _(
            "{host} refused the connection. Check the port."
        ).format(host=host)
    if isinstance(exc, TimeoutError):
        return CATEGORY_UNREACHABLE, _("Connecting to {host} timed out.").format(
            host=host
        )
    if isinstance(exc, (ImapError, SmtpError)):
        text = str(exc).lower()
        if any(hint in text for hint in _AUTH_HINTS):
            return CATEGORY_AUTH, _("Sign-in failed. Check the account password.")
        return CATEGORY_SERVER, str(exc)
    if isinstance(exc, OSError):
        return CATEGORY_UNREACHABLE, _(
            "Couldn't reach the mail server. Check your connection."
        )
    return CATEGORY_SERVER, str(exc)


# Trailing punctuation is sentence, not URL: "see https://x/y." keeps the dot out.
_URL_PATTERN = re.compile(r"https?://[^\s<>]*[^\s<>.,;:)\]]")


# Server messages often carry a help URL ("Application-specific password
# required: https://support.google.com/..."). The banner renders Pango markup,
# so escape the text first, then turn bare URLs into clickable links.
def linkify(text: str) -> str:
    escaped = html.escape(text, quote=False)
    return _URL_PATTERN.sub(lambda m: f'<a href="{m.group()}">{m.group()}</a>', escaped)
