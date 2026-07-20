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
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gdk, Gio, Gtk

from .core.store.database import Database
from .preferences_dialog import PostcardPreferencesDialog
from .window import PostcardMainWindow


class PostcardApplication(Adw.Application):
    __gtype_name__ = "PostcardApplication"

    def __init__(self, version: str) -> None:
        super().__init__(
            application_id="in.gxanshu.postcard",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
            resource_base_path="/in/gxanshu/postcard",
        )
        self.version = version
        self.db = Database()
        self.settings = Gio.Settings(schema_id="in.gxanshu.postcard")

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

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        self._load_css()

    def do_activate(self) -> None:
        win = self.props.active_window or PostcardMainWindow(
            self, self.db, self.settings
        )
        win.present()

    def _load_css(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_resource("/in/gxanshu/postcard/style.css")
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

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
            application_name="Postcard",
            application_icon="in.gxanshu.postcard",
            developer_name="Anshu",
            translator_credits=_("translator-credits"),
            version=self.version,
            developers=["Anshu", "Chris Pouliot https://github.com/chrispouliot"],
            copyright="© 2026 Anshu",
        )
        about.present(self.props.active_window)

    def on_preferences_action(self, *args: object) -> None:
        dialog = PostcardPreferencesDialog(self.settings)
        dialog.present(self.props.active_window)

    def on_new_window_action(self, *args: object) -> None:
        PostcardMainWindow(self, self.db, self.settings).present()

    def on_shortcuts_action(self, *args: object) -> None:
        builder = Gtk.Builder.new_from_resource(
            "/in/gxanshu/postcard/ui/shortcuts-dialog.ui"
        )
        dialog = cast(Adw.ShortcutsDialog, builder.get_object("shortcuts_dialog"))
        dialog.present(self.props.active_window)
