# application.py
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

from collections.abc import Callable
from gettext import gettext as _
from typing import cast

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gio, Gtk

from .accounts_dialog import PostboxAccountsDialog
from .core.store.database import Database
from .preferences_dialog import PostboxPreferencesDialog
from .window import PostboxMainWindow


class PostboxApplication(Adw.Application):
    __gtype_name__ = "PostboxApplication"

    def __init__(self, version: str) -> None:
        super().__init__(
            application_id="in.gxanshu.postbox",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
            resource_base_path="/in/gxanshu/postbox",
        )
        self.version = version
        self.db = Database()
        self.settings = Gio.Settings(schema_id="in.gxanshu.postbox")

        self._create_action("about", self.on_about_action)
        self._create_action(
            "preferences", self.on_preferences_action, ["<control>comma"]
        )
        self._create_action(
            "new-window", self.on_new_window_action, ["<control><shift>n"]
        )
        self._create_action(
            "shortcuts", self.on_shortcuts_action, ["<control>question"]
        )
        self._create_action("quit", lambda *_: self.quit(), ["<control>q"])
        self._create_action("accounts", self.on_accounts_action)

    def do_activate(self) -> None:
        win = self.props.active_window or PostboxMainWindow(
            self, self.db, self.settings
        )
        win.present()

    # Register an app.<name> action, optionally with keyboard accelerators.
    def _create_action(
        self,
        name: str,
        callback: Callable[..., None],
        shortcuts: list[str] | None = None,
    ) -> None:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)
        if shortcuts:
            self.set_accels_for_action(f"app.{name}", shortcuts)

    def on_about_action(self, *args: object) -> None:
        about = Adw.AboutDialog(
            application_name="Postbox",
            application_icon="in.gxanshu.postbox",
            developer_name="Anshu",
            translator_credits=_("translator-credits"),
            version=self.version,
            developers=["Anshu"],
            copyright="© 2026 Anshu",
        )
        about.present(self.props.active_window)

    def on_preferences_action(self, *args: object) -> None:
        dialog = PostboxPreferencesDialog(self.settings)
        dialog.present(self.props.active_window)

    def on_new_window_action(self, *args: object) -> None:
        PostboxMainWindow(self, self.db, self.settings).present()

    def on_shortcuts_action(self, *args: object) -> None:
        builder = Gtk.Builder.new_from_resource(
            "/in/gxanshu/postbox/ui/shortcuts-dialog.ui"
        )
        dialog = cast(Adw.ShortcutsDialog, builder.get_object("shortcuts_dialog"))
        dialog.present(self.props.active_window)

    def on_accounts_action(self, *args: object) -> None:
        dialog = PostboxAccountsDialog(self.db)
        window = self.props.active_window
        if isinstance(window, PostboxMainWindow):
            dialog.connect("closed", lambda *_: window.reload_accounts())
        dialog.present(window)
