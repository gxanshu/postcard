from datetime import UTC, datetime

import pytest

from postcard.core.crypto.subprocess_backend import (
    SubprocessBackend,
    _clean_message,
    _map_verify_error,
    _parse_x509_date,
)
from postcard.core.crypto.types import SignatureStatus


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("verify error:num=10:certificate has expired", SignatureStatus.EXPIRED),
        ("verify error:num=9:certificate is not yet valid", SignatureStatus.EXPIRED),
        ("verify error:num=23:certificate revoked", SignatureStatus.REVOKED),
        (
            "verify error:num=20:unable to get local issuer certificate",
            SignatureStatus.UNTRUSTED,
        ),
        ("verify error:num=18:self signed certificate", SignatureStatus.UNTRUSTED),
        (
            "verify error:num=19:self signed certificate in certificate chain",
            SignatureStatus.UNTRUSTED,
        ),
        (
            "verify error:num=21:unable to verify the first certificate",
            SignatureStatus.UNTRUSTED,
        ),
        ("some random output without known error", SignatureStatus.UNTRUSTED),
    ],
)
def test_map_verify_error(output: str, expected: SignatureStatus) -> None:
    assert _map_verify_error(output) == expected


def test_clean_message_extracts_verify_errors() -> None:
    output = (
        "Verification failure\n"
        "140735...:error:...:verify error:num=20:"
        "unable to get local issuer certificate\n"
        "another line"
    )
    assert _clean_message(output) == "unable to get local issuer certificate"


def test_clean_message_keeps_non_verification_lines() -> None:
    output = "Some other error\nAnother line"
    assert _clean_message(output) == "Some other error\nAnother line"


def test_clean_message_returns_empty_for_success() -> None:
    assert _clean_message("Verification successful") == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "Jul  6 04:53:37 2026 GMT",
            datetime(2026, 7, 6, 4, 53, 37, tzinfo=UTC),
        ),
        (
            "Jul 16 04:53:37 2026 GMT",
            datetime(2026, 7, 16, 4, 53, 37, tzinfo=UTC),
        ),
        (
            "Jul 16 04:53:37 2026 UTC",
            datetime(2026, 7, 16, 4, 53, 37, tzinfo=UTC),
        ),
    ],
)
def test_parse_x509_date(value: str, expected: datetime) -> None:
    assert _parse_x509_date(value) == expected


def test_parse_x509_date_returns_none_for_garbage() -> None:
    assert _parse_x509_date("not a date") is None


def test_find_openssl_returns_absolute_path() -> None:
    backend = SubprocessBackend()
    assert backend._openssl.endswith("openssl")  # noqa: SLF001
