from gi.repository import Adw, GObject, Gtk

from .core import providers, secrets
from .core.models.account import SECURITY_OPTIONS, parse_port
from .core.store.database import Database


@Gtk.Template(resource_path="/in/gxanshu/postcard/ui/account-dialog.ui")
class PostcardAccountDialog(Adw.Dialog):
    __gtype_name__ = "PostcardAccountDialog"

    cancel_button: Gtk.Button = Gtk.Template.Child()
    add_button: Gtk.Button = Gtk.Template.Child()
    display_name_row: Adw.EntryRow = Gtk.Template.Child()
    email_row: Adw.EntryRow = Gtk.Template.Child()
    password_row: Adw.PasswordEntryRow = Gtk.Template.Child()
    imap_host_row: Adw.EntryRow = Gtk.Template.Child()
    imap_port_row: Adw.EntryRow = Gtk.Template.Child()
    smtp_host_row: Adw.EntryRow = Gtk.Template.Child()
    smtp_port_row: Adw.EntryRow = Gtk.Template.Child()
    imap_security_row: Adw.ComboRow = Gtk.Template.Child()
    smtp_security_row: Adw.ComboRow = Gtk.Template.Child()

    __gsignals__ = {
        "account-added": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, db: Database) -> None:
        super().__init__()
        self._db = db

        self.cancel_button.connect("clicked", lambda _b: self.close())
        self.add_button.connect("clicked", self._on_add_clicked)

        # Values we put in the server fields ourselves, so a later autofill can
        # tell its own text from something the user typed and never clobber it.
        # Seeded with the template's defaults, which count as ours.
        self._autofilled_text = {
            row: row.get_text()
            for row in (
                self.imap_host_row,
                self.imap_port_row,
                self.smtp_host_row,
                self.smtp_port_row,
            )
        }
        self._autofilled_security = {
            combo: combo.get_selected()
            for combo in (self.imap_security_row, self.smtp_security_row)
        }
        self.email_row.connect("changed", self._autofill_servers)

        for row in (
            self.display_name_row,
            self.email_row,
            self.password_row,
            self.imap_host_row,
            self.smtp_host_row,
            self.imap_port_row,
            self.smtp_port_row,
        ):
            row.connect("changed", self._update_add_sensitivity)

    def _autofill_servers(self, *_args: object) -> None:
        settings = providers.settings_for_email(self.email_row.get_text())
        if settings is None:
            return

        for row, value in (
            (self.imap_host_row, settings.imap_host),
            (self.imap_port_row, str(settings.imap_port)),
            (self.smtp_host_row, settings.smtp_host),
            (self.smtp_port_row, str(settings.smtp_port)),
        ):
            if row.get_text() == self._autofilled_text[row]:
                row.set_text(value)
                self._autofilled_text[row] = value

        for combo, security in (
            (self.imap_security_row, settings.imap_security),
            (self.smtp_security_row, settings.smtp_security),
        ):
            selected = SECURITY_OPTIONS.index(security)
            if combo.get_selected() == self._autofilled_security[combo]:
                combo.set_selected(selected)
                self._autofilled_security[combo] = selected

    def _update_add_sensitivity(self, *_args: object) -> None:
        required = (
            self.display_name_row.get_text(),
            self.email_row.get_text(),
            self.password_row.get_text(),
            self.imap_host_row.get_text(),
            self.smtp_host_row.get_text(),
        )
        # The ports are validated too, not just non-empty: _on_add_clicked has
        # to parse them, and a blank or non-numeric entry used to raise
        # ValueError inside the clicked handler, where PyGObject swallows it to
        # stderr and Add just appears to do nothing.
        ports_are_valid = (
            parse_port(self.imap_port_row.get_text()) is not None
            and parse_port(self.smtp_port_row.get_text()) is not None
        )

        self.add_button.set_sensitive(
            ports_are_valid and all(field.strip() for field in required)
        )

    def _on_add_clicked(self, _button: Gtk.Button) -> None:
        imap_port = parse_port(self.imap_port_row.get_text())
        smtp_port = parse_port(self.smtp_port_row.get_text())
        if imap_port is None or smtp_port is None:
            # _update_add_sensitivity keeps Add insensitive in this state, so
            # this is belt-and-braces against a programmatic activation.
            return

        account = self._db.save_account(
            email=self.email_row.get_text().strip(),
            display_name=self.display_name_row.get_text().strip(),
            imap_host=self.imap_host_row.get_text().strip(),
            imap_port=imap_port,
            imap_security=SECURITY_OPTIONS[self.imap_security_row.get_selected()],
            smtp_host=self.smtp_host_row.get_text().strip(),
            smtp_port=smtp_port,
            smtp_security=SECURITY_OPTIONS[self.smtp_security_row.get_selected()],
        )

        secrets.store_password(account.id, self.password_row.get_text())
        self.emit("account-added")
        self.close()
