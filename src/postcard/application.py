import logging
from collections.abc import Callable
from gettext import gettext as _
from typing import cast

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .composer_window import composer_for_mailto
from .core.store.database import Database
from .preferences_dialog import PostcardPreferencesDialog
from .tray import Tray
from .window import PostcardMainWindow

logger = logging.getLogger(__name__)

MAILTO_SCHEME = "mailto:"


class PostcardApplication(Adw.Application):
    __gtype_name__ = "PostcardApplication"

    def __init__(self, version: str) -> None:
        super().__init__(
            application_id="in.gxanshu.postcard",
            # HANDLES_OPEN so the desktop can hand us mailto: links.
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
            resource_base_path="/in/gxanshu/postcard",
        )
        self.version = version
        self.db = Database()
        self.settings = Gio.Settings(schema_id="in.gxanshu.postcard")
        self._should_start_hidden = False

        self.tray = Tray(self)

        # For autostart: build the window (so the sync timer runs) but skip
        # presenting it. The Background portal puts this flag in the autostart
        # entry it writes -- see "Start at Login" in preferences_dialog.py.
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
        self.tray.start()
        self.settings.connect(
            "changed::run-in-background", self._on_run_in_background_changed
        )
        self.tray.set_shown(self.settings.get_boolean("run-in-background"))

    def _on_run_in_background_changed(self, settings: Gio.Settings, key: str) -> None:
        self.tray.set_shown(settings.get_boolean(key))

    def _main_window(self) -> PostcardMainWindow | None:
        return next(
            (w for w in self.get_windows() if isinstance(w, PostcardMainWindow)), None
        )

    def do_handle_local_options(self, options: GLib.VariantDict) -> int:
        self._should_start_hidden = options.contains("hidden")
        return Adw.Application.do_handle_local_options(self, options)

    def do_activate(self) -> None:
        win = self._main_window() or PostcardMainWindow(self, self.db, self.settings)
        if self._should_start_hidden:
            # Only the launch activation stays hidden; later ones raise it.
            self._should_start_hidden = False
            return
        win.present()

    def do_open(self, files: list[Gio.File], _n_files: int, _hint: str) -> None:
        for file in files:
            uri = file.get_uri()
            if uri.lower().startswith(MAILTO_SCHEME):
                self._open_mailto(uri)
            else:
                logger.warning("ignoring unsupported URI %s", uri)

    # A mailto: link opens the composer and nothing else; the main window is
    # only presented when there is no account to compose from.
    def _open_mailto(self, uri: str) -> None:
        win = self._main_window()
        if win is not None:
            win.open_mailto(uri)
            return
        accounts = self.db.accounts()
        if not accounts:
            logger.warning("no account to compose %s from, opening the window", uri)
            self.do_activate()
            return
        composer_for_mailto(self, self.db, accounts[0], self.settings, uri).present()

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
        win = self._main_window()
        if win is not None:
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
