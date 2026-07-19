import mimetypes
import threading
from datetime import datetime
from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from . import mail_send
from .core import compose, secrets
from .core.models.account import Account
from .core.models.attachment import Attachment
from .core.store.database import Database


@Gtk.Template(resource_path="/in/gxanshu/postbox/ui/composer-window.ui")
class PostboxComposerWindow(Adw.Window):
    __gtype_name__ = "PostboxComposerWindow"

    cancel_button: Gtk.Button = Gtk.Template.Child()
    send_button: Gtk.Button = Gtk.Template.Child()
    send_spinner: Gtk.Spinner = Gtk.Template.Child()
    to_row: Adw.EntryRow = Gtk.Template.Child()
    subject_row: Adw.EntryRow = Gtk.Template.Child()
    body_view: Gtk.TextView = Gtk.Template.Child()
    attach_button: Gtk.Button = Gtk.Template.Child()
    attachments_list: Gtk.ListBox = Gtk.Template.Child()
    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()

    __gsignals__ = {
        "finished": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(
        self,
        app: Gtk.Application,
        db: Database,
        account: Account,
        to: str = "",
        subject: str = "",
        body: str = "",
    ) -> None:
        super().__init__(application=app)
        self._db = db
        self._account = account
        self._attachments: list[Attachment] = []

        self.to_row.set_text(to)
        self.subject_row.set_text(subject)
        buffer = self.body_view.get_buffer()
        buffer.set_text(body)
        # Land the cursor above any preset signature/quoted text.
        buffer.place_cursor(buffer.get_start_iter())

        self.cancel_button.connect("clicked", self._on_cancel_clicked)
        self.send_button.connect("clicked", self._on_send_clicked)
        self.attach_button.connect("clicked", self._on_attach_clicked)

        for row in (self.to_row, self.subject_row):
            row.connect("changed", self._update_send_sensitivity)
        self.body_view.get_buffer().connect("changed", self._update_send_sensitivity)
        self._update_send_sensitivity()

    def _update_send_sensitivity(self, *_args: object) -> None:
        self.send_button.set_sensitive(
            bool(self.to_row.get_text().strip())
            and bool(self.subject_row.get_text().strip())
        )

    def _body_text(self) -> str:
        buffer = self.body_view.get_buffer()
        start, end = buffer.get_bounds()
        return buffer.get_text(start, end, False)

    def _to_addrs(self) -> list[str]:
        return [
            addr.strip() for addr in self.to_row.get_text().split(",") if addr.strip()
        ]

    def _has_content(self) -> bool:
        return bool(
            self.to_row.get_text().strip()
            or self.subject_row.get_text().strip()
            or self._body_text().strip()
        )

    # --- attachments ---------------------------------------------------

    def _on_attach_clicked(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog()
        dialog.open(self, None, self._on_attach_dialog_done)

    def _on_attach_dialog_done(
        self, dialog: Gtk.FileDialog, result: Gio.AsyncResult
    ) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return  # user cancelled the dialog

        ok, content, _etag = file.load_contents(None)
        if not ok:
            return

        filename = file.get_basename() or "attachment"
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        attachment = Attachment(
            filename=filename, mime_type=mime_type, content=bytes(content)
        )
        self._attachments.append(attachment)
        self._add_attachment_row(attachment)

    def _add_attachment_row(self, attachment: Attachment) -> None:
        row = Adw.ActionRow(title=attachment.filename)
        remove_button = Gtk.Button(
            icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER
        )
        remove_button.add_css_class("flat")
        remove_button.connect(
            "clicked", self._on_remove_attachment_clicked, attachment, row
        )
        row.add_suffix(remove_button)
        self.attachments_list.append(row)

    def _on_remove_attachment_clicked(
        self, _button: Gtk.Button, attachment: Attachment, row: Adw.ActionRow
    ) -> None:
        self._attachments.remove(attachment)
        self.attachments_list.remove(row)

    # --- cancel / save draft ---------------------------------------------

    def _on_cancel_clicked(self, _button: Gtk.Button) -> None:
        if self._has_content():
            folder = self._db.get_or_create_folder(
                self._account.id, "Drafts", "mail-drafts-symbolic"
            )
            msg = compose.build_mime_message(
                self._account.email,
                self._to_addrs(),
                self.subject_row.get_text().strip(),
                self._body_text(),
                self._attachments,
            )
            row = self._db.save_email(
                folder.id,
                sender=self.to_row.get_text().strip() or _("(no recipient)"),
                subject=self.subject_row.get_text().strip() or _("(no subject)"),
                preview=self._body_text()[:100],
                date=_now(),
                unread=False,
            )
            self._db.save_raw_message(row.id, msg.as_bytes())
            self.emit("finished")

        self.close()

    # --- send ------------------------------------------------------------

    def _on_send_clicked(self, _button: Gtk.Button) -> None:
        to_addrs = self._to_addrs()
        subject = self.subject_row.get_text().strip()
        body = self._body_text()

        msg = compose.build_mime_message(
            self._account.email, to_addrs, subject, body, self._attachments
        )
        raw = msg.as_bytes()

        # Save to Outbox before attempting to send -- a crash mid-send can
        # then never lose the message.
        outbox = self._db.get_or_create_folder(
            self._account.id, "Outbox", "mail-outbox-symbolic"
        )
        row = self._db.save_email(
            outbox.id,
            sender=", ".join(to_addrs),
            subject=subject,
            preview=body[:100],
            date=_now(),
            unread=False,
        )
        self._db.save_raw_message(row.id, raw)

        self._set_sending(True)
        thread = threading.Thread(
            target=self._send_worker,
            args=(row.id, subject, to_addrs, raw),
            daemon=True,
        )
        thread.start()

    # Runs on the worker thread: network only, no Gtk/database access.
    def _send_worker(
        self, email_id: int, subject: str, to_addrs: list[str], raw: bytes
    ) -> None:
        password = secrets.lookup_password(self._account.id)
        if not password:
            GLib.idle_add(self._on_send_failed, "no saved password")
            return
        try:
            mail_send.send_message(
                self._account, password, self._account.email, to_addrs, raw
            )
        except Exception as error:
            GLib.idle_add(self._on_send_failed, str(error))
            return
        GLib.idle_add(self._on_send_done, email_id, subject, raw)

    # Back on the main thread: safe to touch the database and widgets.
    def _on_send_done(self, email_id: int, subject: str, raw: bytes) -> bool:
        self._db.delete_email(email_id)
        sent = self._db.get_or_create_folder(
            self._account.id, "Sent", "mail-sent-symbolic"
        )
        row = self._db.save_email(
            sent.id,
            sender=self._account.email,
            subject=subject,
            preview=subject,
            date=_now(),
            unread=False,
        )
        self._db.save_raw_message(row.id, raw)
        self.emit("finished")
        self.close()
        return False

    def _on_send_failed(self, message: str) -> bool:
        self._set_sending(False)
        self.toast_overlay.add_toast(
            Adw.Toast(
                title=_("Couldn't send: {msg}. Saved to Outbox.").format(msg=message)
            )
        )
        self.emit("finished")
        self.close()
        return False

    def _set_sending(self, sending: bool) -> None:
        self.send_button.set_sensitive(not sending)
        self.cancel_button.set_sensitive(not sending)
        self.send_spinner.set_visible(sending)
        if sending:
            self.send_spinner.start()
        else:
            self.send_spinner.stop()


def _now() -> str:
    return datetime.now().strftime("%b %d")
