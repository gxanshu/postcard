import logging
import os
import sys

from .application import PostcardApplication

# Postcard's own logging. Errors that reach the user as a toast or banner are
# also logged here, because a toast is gone the moment it fades and there is
# otherwise nothing to look at after the fact.
#
# NEVER log a worker thread's `args` tuple: the account password is a
# positional item in it. See CLAUDE.md.
_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def _configure_logging() -> None:
    """Log to stderr, which journald captures for the Flatpak.

    WARNING by default so a normal run stays quiet; POSTCARD_LOG=debug (or any
    level name) turns it up without a rebuild. G_MESSAGES_DEBUG only affects
    GLib's own logging, not this.
    """
    # getLevelNamesMapping, not getattr(logging, ...): the latter resolves any
    # attribute on the module, so POSTCARD_LOG=root would hand basicConfig a
    # Logger object and crash on startup.
    requested = os.environ.get("POSTCARD_LOG", "warning").upper()
    level = logging.getLevelNamesMapping().get(requested, logging.WARNING)
    logging.basicConfig(level=level, format=_LOG_FORMAT, stream=sys.stderr)


def main(version: str) -> int:
    _configure_logging()
    app = PostcardApplication(version)
    return app.run(sys.argv)
