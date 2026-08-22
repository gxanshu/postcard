import ipaddress
import ssl

# Constants shared by the IMAP and SMTP sessions. They live here rather than in
# either session module so neither has to import the other.

# Socket timeout for both protocols. Generous on purpose: a first sync over a
# slow link can take a while, and neither imaplib nor smtplib reports partial
# progress, so a tight timeout looks like a connection failure.
NET_TIMEOUT_SECONDS = 30


def _is_loopback(host: str) -> bool:
    """True for 127.0.0.1, ::1 and localhost."""
    stripped = host.strip().strip("[]").lower()
    if stripped in ("localhost", "localhost."):
        return True
    try:
        return ipaddress.ip_address(stripped).is_loopback
    except ValueError:
        return False


def ssl_context_for(host: str) -> ssl.SSLContext:
    """The TLS context both sessions connect with.

    Verifying is the point: imaplib and smtplib were never converted to verify
    by default, so without an explicit context any machine on the path can
    present its own certificate and read the password and the mail.

    Loopback is the exception. A local bridge -- ProtonMail Bridge, hydroxide --
    terminates TLS with a self-signed certificate it generated on the machine
    itself, and there is no path to sit on between two processes on the same
    host, so verifying would reject the one setup that needs no protection.
    """
    context = ssl.create_default_context()
    if _is_loopback(host):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context
