from postcard.core.models.account import (
    MAX_PORT,
    SECURITY_OPTIONS,
    SECURITY_STARTTLS,
    SECURITY_TLS,
    Account,
    parse_port,
)


def account(display_name: str) -> Account:
    return Account(
        id=1,
        email="ada@example.com",
        display_name=display_name,
        imap_host="imap.example.com",
        imap_port=993,
        smtp_host="smtp.example.com",
        smtp_port=587,
    )


def test_parses_a_plain_port() -> None:
    assert parse_port("993") == 993


def test_tolerates_surrounding_whitespace() -> None:
    assert parse_port("  587 ") == 587


def test_returns_none_for_blank_input() -> None:
    # The account dialog's port entry starts populated but can be cleared, and
    # int("") used to raise ValueError inside the Add handler.
    assert parse_port("") is None
    assert parse_port("   ") is None


def test_returns_none_for_non_numeric_input() -> None:
    assert parse_port("smtp") is None
    assert parse_port("99 3") is None
    assert parse_port("993a") is None


def test_returns_none_outside_the_valid_range() -> None:
    assert parse_port("0") is None
    assert parse_port("-1") is None
    assert parse_port(str(MAX_PORT + 1)) is None


def test_accepts_the_range_boundaries() -> None:
    assert parse_port("1") == 1
    assert parse_port(str(MAX_PORT)) == MAX_PORT


def test_security_options_match_the_dialog_combo_order() -> None:
    # account-dialog.blp lists ["TLS", "STARTTLS"] and the dialog indexes this
    # tuple with get_selected(), so the order is load-bearing.
    assert SECURITY_OPTIONS == (SECURITY_TLS, SECURITY_STARTTLS)


def test_short_label_prefers_the_display_name() -> None:
    assert account("Ada").short_label == "Ada"


def test_short_label_falls_back_to_the_local_part() -> None:
    assert account("").short_label == "ada"


def test_short_label_ignores_a_whitespace_only_display_name() -> None:
    assert account("   ").short_label == "ada"
