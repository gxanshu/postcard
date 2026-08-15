import gi

gi.require_version("GdkPixbuf", "2.0")

import hashlib
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path

from gi.repository import GdkPixbuf, GLib

logger = logging.getLogger(__name__)

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

# How long a cached lookup is trusted. Long, because it is mostly caching the
# answer "nobody has one", and finite so a sender who later gets a picture
# eventually shows it.
CACHE_TTL_SECONDS = 30 * 24 * 60 * 60


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
    """The first picture any lookup has for this address, or None.

    Cached on disk between runs, an empty file recording that nobody has one.
    Most senders don't, and that answer otherwise costs three HTTP requests
    every launch -- each of which can sit out the full timeout.
    """
    address = address.strip().lower()
    path = _cache_path(address)
    try:
        if time.time() - path.stat().st_mtime < CACHE_TTL_SECONDS:
            data = path.read_bytes()
            return _decode(data) if data else None
    except OSError:
        pass  # never looked up, or the entry is unreadable -- look it up now

    for provider in (gravatar, favicon):
        image = provider(address)
        if image:
            _write_cache(path, image)
            return image
    _write_cache(path, None)
    return None


def _cache_path(address: str) -> Path:
    digest = hashlib.sha256(address.encode()).hexdigest()
    return Path(GLib.get_user_cache_dir()) / "postcard" / "avatars" / digest


def _write_cache(path: Path, image: GdkPixbuf.Pixbuf | None) -> None:
    """Store the picture, or an empty file recording that there isn't one."""
    try:
        _saved, data = image.save_to_bufferv("png", [], []) if image else (True, b"")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except (OSError, GLib.Error):
        # A full or read-only cache directory costs a lookup next launch, which
        # is not worth failing an avatar over.
        logger.debug("could not cache the avatar at %s", path, exc_info=True)


def _load(url: str) -> GdkPixbuf.Pixbuf | None:
    """Download and decode, or None. Remote input, so nothing here raises."""
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            data = response.read(MAX_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not data or len(data) > MAX_BYTES:
        return None
    return _decode(data)


def _decode(data: bytes) -> GdkPixbuf.Pixbuf | None:
    loader = GdkPixbuf.PixbufLoader()
    try:
        loader.write(data)
        loader.close()
    except GLib.Error:
        return None
    return loader.get_pixbuf()
