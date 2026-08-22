import ssl

from postcard.core.net import ssl_context_for


def test_remote_host_verifies():
    context = ssl_context_for("imap.gmail.com")
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


def test_loopback_skips_verification():
    # ProtonMail Bridge and hydroxide serve a self-signed certificate here.
    for host in ("127.0.0.1", "localhost", "LocalHost", "::1", "[::1]", "127.1.2.3"):
        context = ssl_context_for(host)
        assert context.verify_mode == ssl.CERT_NONE, host
        assert not context.check_hostname, host


def test_hostname_that_merely_mentions_localhost_verifies():
    for host in ("localhost.evil.example", "notlocalhost", "10.0.0.1"):
        assert ssl_context_for(host).verify_mode == ssl.CERT_REQUIRED, host
