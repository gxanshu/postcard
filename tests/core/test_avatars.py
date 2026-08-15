import hashlib

import gi
import pytest

gi.require_version("GdkPixbuf", "2.0")

from gi.repository import GdkPixbuf

from postcard.core import avatars
from postcard.core.avatars import (
    MAX_BYTES,
    _load,
    favicon,
    favicon_urls,
    fetch,
    gravatar,
)


def image(width: int) -> GdkPixbuf.Pixbuf:
    return GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, width, width)


def png(width: int) -> bytes:
    return image(width).save_to_bufferv("png", [], [])[1]


def serve(monkeypatch, *replies: GdkPixbuf.Pixbuf | None) -> list[str]:
    """Answer each successive _load call with the next reply, recording URLs."""
    asked: list[str] = []
    remaining = iter(replies)

    def fake(url: str) -> GdkPixbuf.Pixbuf | None:
        asked.append(url)
        return next(remaining)

    monkeypatch.setattr(avatars, "_load", fake)
    return asked


# --- gravatar ---


def test_gravatar_requests_the_sha256_of_the_address(monkeypatch) -> None:
    asked = serve(monkeypatch, image(128))
    gravatar("ada@example.com")
    assert hashlib.sha256(b"ada@example.com").hexdigest() in asked[0]


def test_gravatar_asks_for_a_404_rather_than_a_placeholder(monkeypatch) -> None:
    # Without d=404 a miss returns a generated image and we could never tell
    # "no avatar" from "has one".
    asked = serve(monkeypatch, image(128))
    gravatar("ada@example.com")
    assert "d=404" in asked[0]


def test_gravatar_skips_anything_that_is_not_an_address(monkeypatch) -> None:
    asked = serve(monkeypatch)
    assert gravatar("not-an-address") is None
    assert asked == []


# --- favicon ---


def test_favicon_tries_duckduckgo_before_google() -> None:
    first, second = favicon_urls("news@stripe.com")
    assert "duckduckgo" in first
    assert "google" in second


@pytest.mark.parametrize(
    "address", ["someone@gmail.com", "someone@outlook.com", "a@proton.me"]
)
def test_favicon_skips_freemail_domains(address: str) -> None:
    assert favicon_urls(address) == []


@pytest.mark.parametrize("address", ["", "no-at-sign", "trailing@", "a@localhost"])
def test_favicon_skips_addresses_without_a_real_domain(address: str) -> None:
    assert favicon_urls(address) == []


def test_favicon_stops_at_the_first_icon_big_enough(monkeypatch) -> None:
    asked = serve(monkeypatch, image(128), image(256))
    assert favicon("a@stripe.com").get_width() == 128
    assert len(asked) == 1


def test_favicon_keeps_looking_past_a_small_icon(monkeypatch) -> None:
    serve(monkeypatch, image(32), image(128))
    assert favicon("a@stripe.com").get_width() == 128


def test_favicon_falls_back_to_the_biggest_of_a_bad_bunch(monkeypatch) -> None:
    serve(monkeypatch, image(32), image(48))
    assert favicon("a@stripe.com").get_width() == 48


def test_favicon_returns_none_when_no_source_has_anything(monkeypatch) -> None:
    serve(monkeypatch, None, None)
    assert favicon("a@stripe.com") is None


# --- fetch ---


def test_fetch_returns_the_first_provider_with_an_image(monkeypatch) -> None:
    def never(address: str) -> None:
        raise AssertionError("providers after a hit must not run")

    monkeypatch.setattr(avatars, "gravatar", lambda a: "picture")
    monkeypatch.setattr(avatars, "favicon", never)
    assert fetch("ada@example.com") == "picture"


def test_fetch_falls_through_to_the_next_provider(monkeypatch) -> None:
    monkeypatch.setattr(avatars, "gravatar", lambda a: None)
    monkeypatch.setattr(avatars, "favicon", lambda a: "logo")
    assert fetch("ada@example.com") == "logo"


def test_fetch_returns_none_when_every_provider_misses(monkeypatch) -> None:
    monkeypatch.setattr(avatars, "gravatar", lambda a: None)
    monkeypatch.setattr(avatars, "favicon", lambda a: None)
    assert fetch("ada@example.com") is None


def test_fetch_normalises_the_address_before_looking_it_up(monkeypatch) -> None:
    # Gravatar only matches on a trimmed, lowercased address.
    seen: list[str] = []
    monkeypatch.setattr(avatars, "gravatar", seen.append)
    monkeypatch.setattr(avatars, "favicon", seen.append)
    fetch("  Ada@Example.COM  ")
    assert seen == ["ada@example.com", "ada@example.com"]


# --- _load ---


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self, size: int) -> bytes:
        return self.body[:size]

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def respond(monkeypatch, body: bytes) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda url, timeout: FakeResponse(body)
    )


def test_load_decodes_a_successful_response(monkeypatch) -> None:
    respond(monkeypatch, png(64))
    assert _load("https://example.com/a.png").get_width() == 64


def test_load_rejects_an_oversized_body(monkeypatch) -> None:
    # A hostile server must not be able to hand us an unbounded "avatar".
    respond(monkeypatch, b"x" * (MAX_BYTES + 1))
    assert _load("https://example.com/a.png") is None


def test_load_rejects_bytes_that_are_not_an_image(monkeypatch) -> None:
    respond(monkeypatch, b"definitely not an image")
    assert _load("https://example.com/a.png") is None


def test_load_swallows_network_errors(monkeypatch) -> None:
    def boom(url: str, timeout: float) -> None:
        raise OSError("connection reset")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert _load("https://example.com/a.png") is None
