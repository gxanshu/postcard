from postcard.core.models.account import SECURITY_STARTTLS, SECURITY_TLS
from postcard.core.providers import settings_for_email


def test_known_domain_is_matched_case_insensitively() -> None:
    settings = settings_for_email("  Someone@GMail.com ")
    assert settings is not None
    assert settings.imap_host == "imap.gmail.com"
    assert settings.imap_port == 993
    assert settings.imap_security == SECURITY_TLS
    assert settings.smtp_host == "smtp.gmail.com"
    assert settings.smtp_port == 587
    assert settings.smtp_security == SECURITY_STARTTLS


def test_unknown_or_incomplete_addresses_return_none() -> None:
    assert settings_for_email("someone@example.invalid") is None
    assert settings_for_email("gmail.com") is None
    assert settings_for_email("") is None


def test_plus_addressing_and_aliases_resolve_to_the_same_provider() -> None:
    assert settings_for_email("a+tag@googlemail.com") == settings_for_email(
        "b@gmail.com"
    )
