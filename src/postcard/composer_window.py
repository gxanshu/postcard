import json
import mimetypes
import threading
from datetime import datetime
from gettext import gettext as _

import gi

gi.require_version("JavaScriptCore", "6.0")
gi.require_version("WebKit", "6.0")

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, JavaScriptCore, WebKit

from . import mail_sync
from .core import compose, secrets
from .core.models.account import Account
from .core.models.attachment import Attachment
from .core.store.database import Database

_EDITOR_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  html, body {{ margin: 0; height: 100%; }}
  body {{
    box-sizing: border-box;
    padding: 12px;
    font-family: {family};
    font-size: {size}pt;
    line-height: 1.5;
    color: #241f31;
    background: transparent;
    outline: none;
  }}
  p {{ margin: 0; }}
  a {{ color: #1c71d8; }}
  blockquote {{
    margin: 0 0 0 2px;
    padding-left: 10px;
    border-left: 2px solid #c0bfbc;
    color: #5e5c64;
  }}
  .signature {{ color: #5e5c64; }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #f6f5f4; }}
    a {{ color: #78aeed; }}
    blockquote {{ border-left-color: #5e5c64; color: #c0bfbc; }}
    .signature {{ color: #c0bfbc; }}
  }}
</style>
</head>
<body contenteditable="true">{body}</body>
<script>
  var COMMANDS = {commands};

  function post() {{
    var states = {{}};
    COMMANDS.forEach(function (name) {{
      states[name] = document.queryCommandState(name);
    }});
    window.webkit.messageHandlers.editor.postMessage(JSON.stringify({{
      html: document.body.innerHTML,
      states: states
    }}));
  }}

  document.addEventListener('input', post);
  document.addEventListener('selectionchange', post);

  // <div> separators inherit no margin, so a sent message keeps the spacing it
  // was typed with even in clients that apply their own stylesheet.
  document.execCommand('defaultParagraphSeparator', false, 'div');
  document.body.focus();

  var first = document.body.firstChild;
  if (first) {{
    var range = document.createRange();
    range.setStart(first, 0);
    range.collapse(true);
    var selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }}
</script>
</html>
"""

_FORMAT_COMMANDS = {
    "bold_button": "bold",
    "italic_button": "italic",
    "underline_button": "underline",
    "strike_button": "strikeThrough",
    "bullets_button": "insertUnorderedList",
    "numbers_button": "insertOrderedList",
}


@Gtk.Template(resource_path="/in/gxanshu/postcard/ui/composer-window.ui")
class PostcardComposerWindow(Adw.Window):
    __gtype_name__ = "PostcardComposerWindow"

    cancel_button: Gtk.Button = Gtk.Template.Child()
    send_button: Gtk.Button = Gtk.Template.Child()
    send_spinner: Gtk.Spinner = Gtk.Template.Child()
    to_row: Adw.EntryRow = Gtk.Template.Child()
    cc_row: Adw.EntryRow = Gtk.Template.Child()
    bcc_row: Adw.EntryRow = Gtk.Template.Child()
    subject_row: Adw.EntryRow = Gtk.Template.Child()
    body_container: Gtk.Box = Gtk.Template.Child()
    bold_button: Gtk.ToggleButton = Gtk.Template.Child()
    italic_button: Gtk.ToggleButton = Gtk.Template.Child()
    underline_button: Gtk.ToggleButton = Gtk.Template.Child()
    strike_button: Gtk.ToggleButton = Gtk.Template.Child()
    bullets_button: Gtk.ToggleButton = Gtk.Template.Child()
    numbers_button: Gtk.ToggleButton = Gtk.Template.Child()
    link_button: Gtk.Button = Gtk.Template.Child()
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
        self._body_html = body or "<div><br></div>"
        self._syncing = False

        self._format_buttons = {
            command: getattr(self, name) for name, command in _FORMAT_COMMANDS.items()
        }

        self.to_row.set_text(to)
        self.subject_row.set_text(subject)
        self._build_editor()

        self.cancel_button.connect("clicked", self._on_cancel_clicked)
        self.send_button.connect("clicked", self._on_send_clicked)
        self.attach_button.connect("clicked", self._on_attach_clicked)
        self.link_button.connect("clicked", self._on_link_clicked)

        for command, button in self._format_buttons.items():
            button.connect("toggled", self._on_format_toggled, command)

        for row in (self.to_row, self.cc_row, self.bcc_row, self.subject_row):
            row.connect("changed", self._update_send_sensitivity)
        self._update_send_sensitivity()

    # --- editor ------------------------------------------------------------

    def _build_editor(self) -> None:
        manager = WebKit.UserContentManager()
        manager.register_script_message_handler("editor", None)
        manager.connect("script-message-received::editor", self._on_editor_changed)

        self._webview = WebKit.WebView(
            user_content_manager=manager, hexpand=True, vexpand=True
        )
        self._webview.get_settings().set_auto_load_images(False)
        # Transparent, so the Adwaita "card" behind it supplies the background
        # and the editor tracks the theme without hardcoding its colours.
        self._webview.set_background_color(Gdk.RGBA(red=0, green=0, blue=0, alpha=0))
        self._webview.load_html(
            _EDITOR_PAGE.format(
                family=_gtk_font_family(),
                size=_gtk_font_size(),
                body=self._body_html,
                commands=json.dumps(list(self._format_buttons)),
            ),
            None,
        )
        self.body_container.append(self._webview)

    def _on_editor_changed(
        self, _manager: WebKit.UserContentManager, value: JavaScriptCore.Value
    ) -> None:
        payload = json.loads(value.to_string())
        self._body_html = payload["html"]

        self._syncing = True
        for command, button in self._format_buttons.items():
            button.set_active(payload["states"].get(command, False))
        self._syncing = False

        self._update_send_sensitivity()

    def _exec(self, command: str, argument: str | None = None) -> None:
        arg = json.dumps(argument) if argument is not None else "null"
        self._webview.evaluate_javascript(
            f"document.execCommand({json.dumps(command)}, false, {arg})",
            -1,
            None,
            None,
            None,
        )

    def _on_format_toggled(self, _button: Gtk.ToggleButton, command: str) -> None:
        if self._syncing:
            return
        self._exec(command)
        self._webview.grab_focus()

    def _on_link_clicked(self, _button: Gtk.Button) -> None:
        entry = Gtk.Entry(placeholder_text="https://", activates_default=True)
        dialog = Adw.AlertDialog(heading=_("Insert Link"), extra_child=entry)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("insert", _("Insert"))
        dialog.set_response_appearance("insert", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("insert")
        dialog.connect("response", self._on_link_response, entry)
        dialog.present(self)

    def _on_link_response(
        self, _dialog: Adw.AlertDialog, response: str, entry: Gtk.Entry
    ) -> None:
        url = entry.get_text().strip()
        if response == "insert" and url:
            self._exec("createLink", url)
        self._webview.grab_focus()

    def _update_send_sensitivity(self, *_args: object) -> None:
        has_recipient = bool(self._to_addrs() or self._cc_addrs() or self._bcc_addrs())
        self.send_button.set_sensitive(
            has_recipient and bool(self.subject_row.get_text().strip())
        )

    def _preview_text(self) -> str:
        return compose.html_to_text(self._body_html)

    @staticmethod
    def _parse_addrs(text: str) -> list[str]:
        return [addr.strip() for addr in text.split(",") if addr.strip()]

    def _to_addrs(self) -> list[str]:
        return self._parse_addrs(self.to_row.get_text())

    def _cc_addrs(self) -> list[str]:
        return self._parse_addrs(self.cc_row.get_text())

    def _bcc_addrs(self) -> list[str]:
        return self._parse_addrs(self.bcc_row.get_text())

    def _recipients_display(self) -> str:
        """A human-readable stand-in for the "sender" column of the Outbox/Drafts
        list, which otherwise has no concept of outgoing recipients."""
        return (
            self.to_row.get_text().strip()
            or self.cc_row.get_text().strip()
            or self.bcc_row.get_text().strip()
            or _("(no recipient)")
        )

    def _has_content(self) -> bool:
        return bool(
            self.to_row.get_text().strip()
            or self.cc_row.get_text().strip()
            or self.bcc_row.get_text().strip()
            or self.subject_row.get_text().strip()
            or self._preview_text()
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
                self._account.id, "Drafts", mail_sync.icon_for_folder("Drafts")
            )
            msg = compose.build_mime_message(
                self._account.email,
                self._to_addrs(),
                self._cc_addrs(),
                self.subject_row.get_text().strip(),
                self._body_html,
                self._attachments,
            )
            row = self._db.save_email(
                folder.id,
                sender=self._recipients_display(),
                subject=self.subject_row.get_text().strip() or _("(no subject)"),
                preview=self._preview_text()[:100],
                date=_now(),
                unread=False,
            )
            self._db.save_raw_message(row.id, msg.as_bytes())
            self.emit("finished")

        self.close()

    # --- send ------------------------------------------------------------

    def _on_send_clicked(self, _button: Gtk.Button) -> None:
        to_addrs = self._to_addrs()
        cc_addrs = self._cc_addrs()
        bcc_addrs = self._bcc_addrs()
        subject = self.subject_row.get_text().strip()

        msg = compose.build_mime_message(
            self._account.email,
            to_addrs,
            cc_addrs,
            subject,
            self._body_html,
            self._attachments,
        )
        raw = msg.as_bytes()

        # The SMTP envelope recipients, unlike the message's own To/Cc
        # headers, also carry Bcc addresses -- they must never appear in the
        # message itself, only in the delivery instructions given to the server.
        recipients = to_addrs + cc_addrs + bcc_addrs

        # Save to Outbox before attempting to send -- a crash mid-send can
        # then never lose the message.
        outbox = self._db.get_or_create_folder(
            self._account.id, "Outbox", mail_sync.icon_for_folder("Outbox")
        )
        row = self._db.save_email(
            outbox.id,
            sender=self._recipients_display(),
            subject=subject,
            preview=self._preview_text()[:100],
            date=_now(),
            unread=False,
        )
        self._db.save_raw_message(row.id, raw)

        self._set_sending(True)
        thread = threading.Thread(
            target=self._send_worker,
            args=(row.id, subject, recipients, raw),
            daemon=True,
        )
        thread.start()

    # Runs on the worker thread: network only, no Gtk/database access.
    def _send_worker(
        self, email_id: int, subject: str, recipients: list[str], raw: bytes
    ) -> None:
        password = secrets.lookup_password(self._account.id)
        if not password:
            GLib.idle_add(self._on_send_failed, "no saved password")
            return
        try:
            mail_sync.send_message(
                self._account, password, self._account.email, recipients, raw
            )
        except Exception as error:
            GLib.idle_add(self._on_send_failed, str(error))
            return
        GLib.idle_add(self._on_send_done, email_id, subject, raw)

    # Back on the main thread: safe to touch the database and widgets.
    def _on_send_done(self, email_id: int, subject: str, raw: bytes) -> bool:
        self._db.delete_email(email_id)
        sent = self._db.get_or_create_folder(
            self._account.id, "Sent", mail_sync.icon_for_folder("Sent")
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


def _gtk_font() -> tuple[str, str]:
    """Split GTK's "Cantarell 11" style font description into family and size."""
    settings = Gtk.Settings.get_default()
    description = (settings and settings.get_property("gtk-font-name")) or "Sans 11"
    family, _, size = description.rpartition(" ")
    if not size.isdigit():
        return description, "11"
    return family, size


def _gtk_font_family() -> str:
    return _gtk_font()[0]


def _gtk_font_size() -> str:
    return _gtk_font()[1]
