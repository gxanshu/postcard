import gi

gi.require_version("GdkPixbuf", "2.0")

import hashlib
import urllib.error
import urllib.request

from gi.repository import GdkPixbuf, GLib

# Shared mail hosts: a favicon here would give every sender the same logo.
FREEMAIL_DOMAINS = frozenset(
    {
        "aol.com",
        "gmail.com",
        "googlemail.com",
        "gmx.com",
        "gmx.de",
        "hotmail.com",
        "icloud.com",
        "live.com",
        "mail.com",
        "me.com",
        "outlook.com",
        "pm.me",
        "proton.me",
        "protonmail.com",
        "yahoo.com",
        "yandex.com",
        "zoho.com",
    }
)

MAX_BYTES = 256 * 1024
MIN_ICON_PX = 64
TIMEOUT = 5.0


def gravatar(address: str) -> GdkPixbuf.Pixbuf | None:
    if "@" not in address:
        return None
    digest = hashlib.sha256(address.encode()).hexdigest()
    return _load(f"https://gravatar.com/avatar/{digest}?s=128&d=404")


def favicon_urls(address: str) -> list[str]:
    domain = address.rpartition("@")[2]
    if "." not in domain or domain in FREEMAIL_DOMAINS:
        return []
    return [
        f"https://icons.duckduckgo.com/ip3/{domain}.ico",
        f"https://www.google.com/s2/favicons?domain={domain}&sz=128",
    ]


def favicon(address: str) -> GdkPixbuf.Pixbuf | None:
    """The largest icon on offer, stopping early once one is big enough."""
    best = None
    for url in favicon_urls(address):
        image = _load(url)
        if image and (best is None or image.get_width() > best.get_width()):
            best = image
        if best is not None and best.get_width() >= MIN_ICON_PX:
            break
    return best


def fetch(address: str) -> GdkPixbuf.Pixbuf | None:
    """The first picture any lookup has for this address, or None."""
    address = address.strip().lower()
    for provider in (gravatar, favicon):
        image = provider(address)
        if image:
            return image
    return None


def _load(url: str) -> GdkPixbuf.Pixbuf | None:
    """Download and decode, or None. Remote input, so nothing here raises."""
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            data = response.read(MAX_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not data or len(data) > MAX_BYTES:
        return None

    loader = GdkPixbuf.PixbufLoader()
    try:
        loader.write(data)
        loader.close()
    except GLib.Error:
        return None
    return loader.get_pixbuf()
