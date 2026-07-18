from gettext import gettext as _

from gi.repository import Adw, Gtk

from .account_dialog import PostboxAccountDialog
from .core import secrets
from .core.store.database import Database


@Gtk.Template(resource_path="/in/gxanshu/postbox/ui/accounts-dialog.ui")
class PostboxAccountsDialog(Adw.Dialog):
    __gtype_name__ = "PostboxAccountsDialog"

    accounts_group: Adw.PreferencesGroup = Gtk.Template.Child()
    add_button: Gtk.Button = Gtk.Template.Child()

    def __init__(self, db: Database) -> None:
        super().__init__()
        self._db = db
        self._rows: list[Adw.ActionRow] = []

        self.add_button.connect("clicked", self._on_add_clicked)
        self._reload()

    def _reload(self) -> None:
        for row in self._rows:
            self.accounts_group.remove(row)
        self._rows.clear()

        for account in self._db.accounts():
            row = Adw.ActionRow(title=account.email, subtitle=account.display_name)

            remove_button = Gtk.Button(
                icon_name="user-trash-symbolic",
                valign=Gtk.Align.CENTER,
                tooltip_text=_("Remove Account"),
            )
            remove_button.add_css_class("flat")
            remove_button.connect("clicked", self._on_remove_clicked, account.id)
            row.add_suffix(remove_button)

            self.accounts_group.add(row)
            self._rows.append(row)

    def _on_remove_clicked(self, _button: Gtk.Button, account_id: int) -> None:
        self._db.delete_account(account_id)
        secrets.clear_password(account_id)
        self._reload()

    def _on_add_clicked(self, _button: Gtk.Button) -> None:
        dialog = PostboxAccountDialog(self._db)
        dialog.connect("account-added", lambda _d: self._reload())
        dialog.present(self)
