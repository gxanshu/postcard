import email
from email import policy
from email.utils import parseaddr
from gettext import gettext as _

from gi.repository import Adw, Gtk

from .account_dialog import PostcardAccountDialog
from .accounts_dialog import PostcardAccountsDialog
from .composer_window import PostcardComposerWindow
from .core import compose
from .core.mime.message_parser import ParsedMessage
from .core.models.account import Account
from .window_parts import MainWindowParts
from .window_types import PAGE_NO_ACCOUNT


# ponytail: quotes are flattened to text; inlining the original's real HTML
# would need a sanitizer, since the composer runs with JavaScript enabled.
def _original_text(parsed: ParsedMessage | None) -> str:
    if parsed is None:
        return ""
    return parsed.text_body or compose.html_to_text(parsed.html_body or "")


class AccountsMixin(MainWindowParts):
    """The account switcher, adding accounts, and opening the composer."""

    def _refresh_account_switcher(self) -> None:
        account = self._account
        if account is None:
            return
        self.account_switcher.set_label(account.email)
        self.account_switcher.set_popover(self._build_account_popover())

    def _build_account_popover(self) -> Gtk.Popover:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for margin in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{margin}")(6)

        accounts_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        accounts_list.add_css_class("boxed-list")
        for account in self._db.accounts():
            row = Adw.ActionRow(
                title=account.email, subtitle=account.display_name, activatable=True
            )
            if self._account is not None and account.id == self._account.id:
                row.add_suffix(Gtk.Image.new_from_icon_name("object-select-symbolic"))
            row.connect("activated", self._on_account_row_activated, account)
            accounts_list.append(row)
        box.append(accounts_list)

        box.append(Gtk.Separator())
        for label, handler in (
            (_("Add Account"), self._on_switcher_add),
            (_("Manage Accounts"), self._on_switcher_manage),
        ):
            button = Gtk.Button(label=label)
            button.add_css_class("flat")
            button.connect("clicked", handler)
            box.append(button)

        popover = Gtk.Popover()
        popover.set_child(box)
        return popover

    def _on_account_row_activated(self, _row: Adw.ActionRow, account: Account) -> None:
        self.account_switcher.popdown()
        if self._account is None or account.id != self._account.id:
            self._load_mail_view(account)

    def _on_switcher_add(self, button: Gtk.Button) -> None:
        self.account_switcher.popdown()
        self._on_add_account_clicked(button)

    def _on_switcher_manage(self, _button: Gtk.Button) -> None:
        self.account_switcher.popdown()
        dialog = PostcardAccountsDialog(self._db)
        dialog.connect("closed", lambda *_: self.reload_accounts())
        dialog.present(self)

    # Re-read accounts after they change (add/remove); fall back sensibly if
    # the active account was deleted.
    def reload_accounts(self) -> None:
        accounts = self._db.accounts()
        if not accounts:
            self.main_stack.set_visible_child_name(PAGE_NO_ACCOUNT)
            return
        current = self._account.id if self._account else None
        if current is not None and any(account.id == current for account in accounts):
            self._refresh_account_switcher()
        else:
            self._load_mail_view(accounts[0])

    def _on_add_account_clicked(self, _button: Gtk.Button) -> None:
        dialog = PostcardAccountDialog(self._db)
        dialog.connect("account-added", self._on_account_added)
        dialog.present(self)

    def _on_account_added(self, _dialog: PostcardAccountDialog) -> None:
        # Load the newly added account (highest id sorts last).
        self._load_mail_view(self._db.accounts()[-1])

    def _signature_text(self) -> str:
        if not self._settings.get_boolean("signature-enabled"):
            return ""
        return self._settings.get_string("signature-text").strip()

    def _on_compose_clicked(self, *_args: object) -> None:
        sig = self._signature_text()
        self._open_composer(body=compose.signature_block(sig) if sig else "")

    def _on_reply_clicked(self, *_args: object) -> None:
        self._open_reply(should_reply_all=False)

    def _on_reply_all_clicked(self, *_args: object) -> None:
        self._open_reply(should_reply_all=True)

    def _open_reply(self, *, should_reply_all: bool) -> None:
        if (
            len(self._selected_conversations()) != 1
            or self._active_view is None
            or self._active_view.raw is None
        ):
            return
        account = self._account
        if account is None:
            return
        headers = email.message_from_bytes(self._active_view.raw, policy=policy.default)
        from_header = str(headers["From"] or "")
        # Reply-To wins over From: it is how a sender asks for replies elsewhere.
        to_addr = parseaddr(str(headers["Reply-To"] or "").strip() or from_header)[1]
        body = compose.quote_reply_body(
            from_header,
            str(headers["Date"] or ""),
            _original_text(self._active_view.parsed),
            signature=self._signature_text(),
        )
        self._open_composer(
            to=to_addr,
            cc=compose.reply_all_cc(headers, account.email, to_addr)
            if should_reply_all
            else "",
            subject=compose.reply_subject(str(headers["Subject"] or "")),
            body=body,
        )

    def _on_forward_clicked(self, *_args: object) -> None:
        if (
            len(self._selected_conversations()) != 1
            or self._active_view is None
            or self._active_view.raw is None
        ):
            return
        headers = email.message_from_bytes(self._active_view.raw, policy=policy.default)
        subject = compose.forward_subject(str(headers["Subject"] or ""))
        parsed = self._active_view.parsed
        body = compose.forward_body(
            str(headers["From"] or ""),
            str(headers["Date"] or ""),
            str(headers["Subject"] or ""),
            _original_text(parsed),
            signature=self._signature_text(),
        )
        self._open_composer(subject=subject, body=body)

    # Open the composer for a mailto: link handed to us by the desktop.
    def open_mailto(self, uri: str) -> None:
        draft = compose.parse_mailto(uri)
        signature = self._signature_text()
        body = draft.body_html or (
            compose.signature_block(signature) if signature else ""
        )
        self._open_composer(
            to=draft.to,
            subject=draft.subject,
            body=body,
            cc=draft.cc,
            bcc=draft.bcc,
        )

    def _open_composer(
        self,
        to: str = "",
        subject: str = "",
        body: str = "",
        cc: str = "",
        bcc: str = "",
    ) -> None:
        account = self._account
        if account is None:
            return
        composer = PostcardComposerWindow(
            self.get_application(),
            self._db,
            account,
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
        )
        composer.connect("finished", self._on_composer_finished)
        composer.present()

    def _on_composer_finished(self, _composer: PostcardComposerWindow) -> None:
        self._reload_folders()
        self._refresh_conversations()
        self._drain_outbox()
