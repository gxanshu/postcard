import logging
from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from .core import goa
from .core.store.database import Database

logger = logging.getLogger(__name__)

PAGE_LIST = "list"
PAGE_EMPTY = "empty"

_SETTINGS_BUS_NAME = "org.gnome.Settings"
_SETTINGS_OBJECT_PATH = "/org/gnome/Settings"
_ONLINE_ACCOUNTS_PANEL = "online-accounts"
# Settings is D-Bus activated, so the first call waits for it to start.
_SETTINGS_TIMEOUT_MS = 30_000


@Gtk.Template(resource_path="/in/gxanshu/postcard/ui/online-accounts-dialog.ui")
class PostcardOnlineAccountsDialog(Adw.Dialog):
    __gtype_name__ = "PostcardOnlineAccountsDialog"

    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()
    accounts_stack: Gtk.Stack = Gtk.Template.Child()
    accounts_group: Adw.PreferencesGroup = Gtk.Template.Child()
    settings_button: Gtk.Button = Gtk.Template.Child()
    empty_settings_button: Gtk.Button = Gtk.Template.Child()

    __gsignals__ = {
        "account-added": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, db: Database) -> None:
        super().__init__()
        self._db = db
        self._rows: list[Adw.ActionRow] = []

        self.settings_button.connect("clicked", self._on_settings_clicked)
        self.empty_settings_button.connect("clicked", self._on_settings_clicked)
        self._reload()

    def _reload(self) -> None:
        for row in self._rows:
            self.accounts_group.remove(row)
        self._rows.clear()

        accounts = goa.mail_accounts()
        self.accounts_stack.set_visible_child_name(
            PAGE_LIST if accounts else PAGE_EMPTY
        )

        # By address, not goa_id: that also catches the same mailbox already
        # added by hand, which would otherwise sync into a second folder tree.
        in_use = {account.email for account in self._db.accounts()}
        for online in accounts:
            row = Adw.ActionRow(title=online.email, subtitle=online.provider_name)
            if not online.is_mail_supported:
                # Microsoft 365 is the one people hit: its token only covers the
                # Graph API, so there is no IMAP server to point at.
                row.set_subtitle(
                    _("{provider} accounts don't allow IMAP mail access").format(
                        provider=online.provider_name
                    )
                )
                row.set_sensitive(False)
            elif not online.is_oauth2:
                row.set_subtitle(_("Use Add Account to set this one up"))
                row.set_sensitive(False)
            elif online.email in in_use:
                row.add_suffix(Gtk.Label(label=_("Added"), valign=Gtk.Align.CENTER))
                row.set_sensitive(False)
            else:
                add_button = Gtk.Button(label=_("Add"), valign=Gtk.Align.CENTER)
                add_button.add_css_class("suggested-action")
                add_button.connect("clicked", self._on_add_clicked, online)
                row.add_suffix(add_button)

            self.accounts_group.add(row)
            self._rows.append(row)

    def _on_add_clicked(self, _button: Gtk.Button, online: goa.OnlineAccount) -> None:
        self._db.save_account(
            email=online.email,
            display_name=online.display_name,
            imap_host=online.imap_host,
            imap_port=online.imap_port,
            imap_security=online.imap_security,
            smtp_host=online.smtp_host,
            smtp_port=online.smtp_port,
            smtp_security=online.smtp_security,
            goa_id=online.goa_id,
        )
        self._reload()
        self.emit("account-added")

    def _on_settings_clicked(self, _button: Gtk.Button) -> None:
        # Called rather than fired through a Gio.DBusActionGroup so that a
        # missing or unreachable Settings comes back as an error we can show,
        # instead of a button that silently does nothing.
        panel = GLib.Variant("(sav)", (_ONLINE_ACCOUNTS_PANEL, []))
        Gio.bus_get_sync(Gio.BusType.SESSION, None).call(
            _SETTINGS_BUS_NAME,
            _SETTINGS_OBJECT_PATH,
            "org.freedesktop.Application",
            "ActivateAction",
            GLib.Variant("(sava{sv})", ("launch-panel", [panel], {})),
            None,
            Gio.DBusCallFlags.NONE,
            _SETTINGS_TIMEOUT_MS,
            None,
            self._on_settings_opened,
        )

    def _on_settings_opened(
        self, connection: Gio.DBusConnection, result: Gio.AsyncResult
    ) -> None:
        try:
            connection.call_finish(result)
        except GLib.Error:
            logger.warning("could not open the Online Accounts panel", exc_info=True)
            self.toast_overlay.add_toast(Adw.Toast(title=_("Could not open Settings.")))
