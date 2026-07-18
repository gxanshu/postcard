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
from .accounts_dialog import PostboxAcountsDialog

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gio, Gtk

from .core.store.database import Database
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

        self._create_action("about", self.on_about_action)
        self._create_action("preferences", self.on_preferences_action)
        self._create_action("new-window", self.on_new_window_action, ["<control>n"])
        self._create_action(
            "shortcuts", self.on_shortcuts_action, ["<control>question"]
        )
        self._create_action("quit", lambda *_: self.quit(), ["<control>q"])
        self._create_action("accounts", self.on_acounts_action)

    def do_activate(self) -> None:
        win = self.props.active_window or PostboxMainWindow(self, self.db)
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
        print("app.preferences action activated")

    def on_new_window_action(self, *args: object) -> None:
        PostboxMainWindow(self, self.db).present()

    def on_shortcuts_action(self, *args: object) -> None:
        builder = Gtk.Builder.new_from_resource(
            "/in/gxanshu/postbox/ui/shortcuts-dialog.ui"
        )
        dialog = builder.get_object("shortcuts_dialog")
        dialog.present(self.props.active_window)

    def on_acounts_action(self, *args: object) -> None:
        dialog = PostboxAcountsDialog(self.db)
        dialog.present(self.props.active_window)
