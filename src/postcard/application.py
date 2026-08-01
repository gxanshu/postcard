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

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

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
        self._start_hidden = False

        # For autostart: build the window (so the sync timer runs) but skip
        # presenting it. See the autostart .desktop file in the README.
        self.add_main_option(
            "hidden",
            0,
            GLib.OptionFlags.NONE,
            GLib.OptionArg.NONE,
            _("Start in the background without showing a window"),
            None,
        )

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
        self._create_action("focus-mail", lambda *_: self.do_activate())
        self._create_action(
            "open-mail", self.on_open_mail, param_type=GLib.VariantType.new("(is)")
        )

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        self._load_css()

    def do_handle_local_options(self, options: GLib.VariantDict) -> int:
        self._start_hidden = options.contains("hidden")
        return Adw.Application.do_handle_local_options(self, options)

    def do_activate(self) -> None:
        win = self.props.active_window or PostcardMainWindow(
            self, self.db, self.settings
        )
        if self._start_hidden:
            # Only the launch activation stays hidden; later ones raise it.
            self._start_hidden = False
            return
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
        param_type: GLib.VariantType | None = None,
    ) -> None:
        action = Gio.SimpleAction.new(name, param_type)
        action.connect("activate", callback)
        self.add_action(action)
        if shortcuts:
            self.set_accels_for_action(f"app.{name}", shortcuts)

    MAINTAINERS = [
        "Anshu Meena https://github.com/gxanshu",
    ]

    def on_about_action(self, *_args: object) -> None:
        about = Adw.AboutDialog.new_from_appdata(
            "/in/gxanshu/postcard/metainfo.xml", self.version
        )
        about.set_translator_credits(_("translator-credits"))
        about.set_developers(self.MAINTAINERS)
        about.set_copyright("© 2026 Anshu")
        about.present(self.props.active_window)

    def on_preferences_action(self, *_args: object) -> None:
        dialog = PostcardPreferencesDialog(self.settings)
        dialog.present(self.props.active_window)

    def on_open_mail(self, _action: Gio.SimpleAction, param: GLib.Variant) -> None:
        self.do_activate()
        win = self.props.active_window
        if isinstance(win, PostcardMainWindow):
            folder_id, uid = param.unpack()
            win.open_email(folder_id, uid)

    def on_new_window_action(self, *_args: object) -> None:
        PostcardMainWindow(self, self.db, self.settings).present()

    def on_shortcuts_action(self, *_args: object) -> None:
        builder = Gtk.Builder.new_from_resource(
            "/in/gxanshu/postcard/ui/shortcuts-dialog.ui"
        )
        dialog = cast(Adw.ShortcutsDialog, builder.get_object("shortcuts_dialog"))
        dialog.present(self.props.active_window)
