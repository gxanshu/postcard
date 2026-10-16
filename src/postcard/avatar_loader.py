import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from gi.repository import Gdk, GdkPixbuf, Gio, GLib

from .core import avatars

logger = logging.getLogger(__name__)

MAX_WORKERS = 4

# ponytail: clears wholesale rather than evicting; an LRU if churn shows up.
MAX_CACHED = 500

OnReady = Callable[[Gdk.Texture | None], None]


class AvatarLoader:
    """Address to texture, fetched in the background and cached in memory.

    Nothing is written to disk. A cached None means nobody had a picture, so
    a sender without one costs a single request per session. `on_ready` is
    always called, with None when there is no picture or avatars are off.
    """

    def __init__(self, settings: Gio.Settings) -> None:
        self._settings = settings
        self._pool = ThreadPoolExecutor(
            max_workers=MAX_WORKERS, thread_name_prefix="avatar"
        )
        self._cache: dict[str, Gdk.Texture | None] = {}
        self._waiting_callbacks: dict[str, list[OnReady]] = {}

    def load(self, address: str, on_ready: OnReady) -> None:
        address = address.strip().lower()
        if not address or not self._settings.get_boolean("load-sender-avatars"):
            on_ready(None)
        elif address in self._cache:
            on_ready(self._cache[address])
        elif address in self._waiting_callbacks:
            self._waiting_callbacks[address].append(on_ready)
        else:
            self._waiting_callbacks[address] = [on_ready]
            self._pool.submit(self._worker, address)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _worker(self, address: str) -> None:
        try:
            image = avatars.fetch(address)
        except Exception:
            # avatars.fetch already returns None for every expected failure
            # (404, timeout, not an image), so reaching here means a bug rather
            # than a missing avatar -- log it instead of hiding it. Still
            # swallowed: the address must never be left stuck in _waiting.
            logger.exception("avatar lookup failed for %s", address)
            image = None
        GLib.idle_add(self._deliver, address, image)

    def _deliver(self, address: str, image: GdkPixbuf.Pixbuf | None) -> bool:
        texture = Gdk.Texture.new_for_pixbuf(image) if image else None
        if len(self._cache) >= MAX_CACHED:
            self._cache.clear()
        self._cache[address] = texture

        for on_ready in self._waiting_callbacks.pop(address, []):
            on_ready(texture)
        return GLib.SOURCE_REMOVE
