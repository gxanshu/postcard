# main.py
#
# Copyright 2026 Anshu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

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
