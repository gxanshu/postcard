from gi.repository import Adw, GObject, Gtk

from .core import secrets
from .core.store.database import Database


@Gtk.Template(resource_path="/in/gxanshu/postbox/ui/account-dialog.ui")
class PostboxAccountDialog(Adw.Dialog):
    __gtype_name__ = "PostboxAccountDialog"

    cancel_button: Gtk.Button = Gtk.Template.Child()
    add_button: Gtk.Button = Gtk.Template.Child()
    display_name_row: Adw.EntryRow = Gtk.Template.Child()
    email_row: Adw.EntryRow = Gtk.Template.Child()
    password_row: Adw.PasswordEntryRow = Gtk.Template.Child()
    imap_host_row: Adw.EntryRow = Gtk.Template.Child()
    imap_port_row: Adw.EntryRow = Gtk.Template.Child()
    smtp_host_row: Adw.EntryRow = Gtk.Template.Child()
    smtp_port_row: Adw.EntryRow = Gtk.Template.Child()

    __gsignals__ = {
        "account-added": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, db: Database) -> None:
        super().__init__()
        self._db = db

        self.cancel_button.connect("clicked", lambda _b: self.close())
        self.add_button.connect("clicked", self._on_add_clicked)

        for row in (
            self.display_name_row,
            self.email_row,
            self.password_row,
            self.imap_host_row,
            self.smtp_host_row,
        ):
            row.connect("changed", self._update_add_sensitivity)

    def _update_add_sensitivity(self, *_args: object) -> None:
        required = (
            self.display_name_row.get_text(),
            self.email_row.get_text(),
            self.password_row.get_text(),
            self.imap_host_row.get_text(),
            self.smtp_host_row.get_text(),
        )

        self.add_button.set_sensitive(all(field.strip() for field in required))

    def _on_add_clicked(self, _button: Gtk.Button) -> None:
        account = self._db.save_account(
            email=self.email_row.get_text().strip(),
            display_name=self.display_name_row.get_text().strip(),
            imap_host=self.imap_host_row.get_text().strip(),
            imap_port=int(self.imap_port_row.get_text().strip()),
            smtp_host=self.smtp_host_row.get_text().strip(),
            smtp_port=int(self.smtp_port_row.get_text().strip()),
        )

        secrets.store_password(account.id, self.password_row.get_text())
        self.emit("account-added")
        self.close()
