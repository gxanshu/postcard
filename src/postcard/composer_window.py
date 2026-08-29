import json
import logging
import mimetypes
import threading
from datetime import datetime
from email.utils import getaddresses
from gettext import gettext as _

import gi

gi.require_version("JavaScriptCore", "6.0")
gi.require_version("WebKit", "6.0")

from gi.repository import (
    Adw,
    Gdk,
    Gio,
    GLib,
    GObject,
    Gtk,
    JavaScriptCore,
    Pango,
    WebKit,
)

from . import mail_sync
from .core import compose, secrets
from .core.models.account import Account
from .core.models.attachment import Attachment
from .core.net import errors
from .core.store.database import Database
from .core.threader import NO_SUBJECT

logger = logging.getLogger(__name__)

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


class _AddressSuggestions:
    """A drop-down of known addresses under one recipient row.

    Gtk.EntryCompletion only attaches to a Gtk.Entry, and an Adw.EntryRow is a
    list box row, so the popover is driven by hand.
    """

    def __init__(self, row: Adw.EntryRow, addresses: list[str]) -> None:
        self._row = row
        self._addresses = addresses
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE)
        # autohide would steal focus from the entry the moment it pops up.
        self._popover = Gtk.Popover(
            child=self._list,
            autohide=False,
            has_arrow=False,
            position=Gtk.PositionType.BOTTOM,
        )
        self._popover.set_parent(row)

        row.connect("changed", self._on_changed)
        row.connect("destroy", lambda *_: self._popover.unparent())
        self._list.connect("row-activated", self._on_row_activated)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        row.add_controller(keys)

    def _on_changed(self, _row: Adw.EntryRow) -> None:
        matches = compose.suggest_addresses(self._row.get_text(), self._addresses)
        self._list.remove_all()
        for address in matches:
            self._list.append(
                Gtk.Label(
                    label=address,
                    xalign=0,
                    ellipsize=Pango.EllipsizeMode.END,
                    max_width_chars=40,
                    margin_top=8,
                    margin_bottom=8,
                    margin_start=12,
                    margin_end=12,
                )
            )

        if matches:
            self._list.select_row(self._list.get_row_at_index(0))
            # Line the drop-down up with the row it belongs to.
            self._popover.set_size_request(self._row.get_width(), -1)
            self._popover.popup()
        else:
            self._popover.popdown()

    def _on_row_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        label = row.get_child()
        assert isinstance(label, Gtk.Label)
        # The trailing ", " leaves nothing being typed, so the popover closes
        # itself on the resulting "changed" -- and shows how to add another.
        self._row.set_text(
            compose.replace_last_address(self._row.get_text(), label.get_label())
        )
        self._row.set_position(-1)

    def _on_key_pressed(
        self, _controller: Gtk.EventControllerKey, keyval: int, _code: int, _state: int
    ) -> bool:
        if not self._popover.get_visible():
            return False

        if keyval == Gdk.KEY_Escape:
            self._popover.popdown()
        elif keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Tab):
            selected = self._list.get_selected_row()
            if selected is None:
                return False
            self._on_row_activated(self._list, selected)
        elif keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            self._move_selection(1 if keyval == Gdk.KEY_Down else -1)
        else:
            return False
        return True

    def _move_selection(self, step: int) -> None:
        selected = self._list.get_selected_row()
        index = (selected.get_index() if selected else 0) + step
        row = self._list.get_row_at_index(index)
        if row is not None:
            self._list.select_row(row)


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
    from_row: Adw.ComboRow = Gtk.Template.Child()
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
    attachments_group: Gtk.Box = Gtk.Template.Child()
    attachments_list: Gtk.ListBox = Gtk.Template.Child()
    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()

    __gsignals__ = {
        "finished": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(
        self,
        app: Gtk.Application | None,
        db: Database,
        account: Account,
        to: str = "",
        subject: str = "",
        body: str = "",
        cc: str = "",
        bcc: str = "",
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

        self._build_from_row(account)
        self.to_row.set_text(to)
        self.cc_row.set_text(cc)
        self.bcc_row.set_text(bcc)
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

        known = db.contact_addresses()
        self._suggestions = [
            _AddressSuggestions(row, known)
            for row in (self.to_row, self.cc_row, self.bcc_row)
        ]

    # The account every send, draft and Sent copy belongs to. Picking another
    # here is the only way to change it once the composer is open.
    def _build_from_row(self, account: Account) -> None:
        self._accounts = self._db.accounts()
        self.from_row.set_model(
            Gtk.StringList(strings=[each.email for each in self._accounts])
        )
        ids = [each.id for each in self._accounts]
        self.from_row.set_selected(ids.index(account.id) if account.id in ids else 0)
        self.from_row.connect("notify::selected", self._on_from_changed)

    def _on_from_changed(self, *_args: object) -> None:
        self._account = self._accounts[self.from_row.get_selected()]

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
        family, size = _gtk_font()
        self._webview.load_html(
            _EDITOR_PAGE.format(
                family=family,
                size=size,
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
            icon_name="window-close-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text=_("Remove Attachment"),
        )
        remove_button.add_css_class("flat")
        remove_button.connect(
            "clicked", self._on_remove_attachment_clicked, attachment, row
        )
        row.add_suffix(remove_button)
        self.attachments_list.append(row)
        self.attachments_group.set_visible(True)

    def _on_remove_attachment_clicked(
        self, _button: Gtk.Button, attachment: Attachment, row: Adw.ActionRow
    ) -> None:
        self._attachments.remove(attachment)
        self.attachments_list.remove(row)
        self.attachments_group.set_visible(bool(self._attachments))

    # --- cancel / save draft ---------------------------------------------

    def _on_cancel_clicked(self, _button: Gtk.Button) -> None:
        if self._has_content():
            folder = self._db.get_or_create_folder(
                self._account.id,
                mail_sync.DRAFTS_FOLDER,
                mail_sync.icon_for_folder(mail_sync.DRAFTS_FOLDER),
            )
            msg = compose.build_mime_message(
                self._account.email,
                self._to_addrs(),
                self._cc_addrs(),
                self.subject_row.get_text().strip(),
                self._body_html,
                self._attachments,
            )
            recipient, recipient_address = mail_sync.first_recipient(
                self.to_row.get_text()
            )
            row = self._db.save_email(
                folder.id,
                sender=self._recipients_display(),
                recipient=recipient,
                recipient_address=recipient_address,
                subject=self.subject_row.get_text().strip() or NO_SUBJECT,
                preview=self._preview_text()[:100],
                date=_now(),
                is_unread=False,
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

        # Bcc is never written to a received message, so this is the only
        # place a Bcc'd address can be learned.
        self._db.save_contacts(getaddresses(to_addrs + cc_addrs + bcc_addrs))

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
            self._account.id,
            mail_sync.OUTBOX_FOLDER,
            mail_sync.icon_for_folder(mail_sync.OUTBOX_FOLDER),
        )
        recipient, recipient_address = mail_sync.first_recipient(self.to_row.get_text())
        row = self._db.save_email(
            outbox.id,
            sender=self._recipients_display(),
            recipient=recipient,
            recipient_address=recipient_address,
            subject=subject,
            preview=self._preview_text()[:100],
            date=_now(),
            is_unread=False,
        )
        self._db.save_raw_message(row.id, raw)

        self._set_sending(True)
        thread = threading.Thread(
            target=self._send_worker,
            args=(self._account, row.id, subject, recipients, raw),
            daemon=True,
        )
        thread.start()

    # Runs on the worker thread: network only, no Gtk/database access.
    def _send_worker(
        self,
        account: Account,
        email_id: int,
        subject: str,
        recipients: list[str],
        raw: bytes,
    ) -> None:
        credential = secrets.credential_for(account)
        if credential is None:
            logger.warning("could not sign in to account %s", account.email)
            GLib.idle_add(self._on_send_failed, _("Could not sign in to this account."))
            return

        try:
            mail_sync.send_message(account, credential, account.email, recipients, raw)
        except Exception as error:
            logger.exception(
                "could not send %r to %s via %s (account %s)",
                subject,
                ", ".join(recipients),
                account.smtp_host,
                account.email,
            )
            _is_auth_failure, message = errors.classify(error, account.smtp_host)
            GLib.idle_add(self._on_send_failed, message)
            return
        GLib.idle_add(self._on_send_done, account, email_id, subject, raw)

    # Back on the main thread: safe to touch the database and widgets.
    def _on_send_done(
        self, account: Account, email_id: int, subject: str, raw: bytes
    ) -> bool:
        self._db.delete_email(email_id)
        sent = mail_sync.sent_folder(self._db, account.id)
        recipient, recipient_address = mail_sync.first_recipient(self.to_row.get_text())
        row = self._db.save_email(
            sent.id,
            sender=account.email,
            recipient=recipient,
            recipient_address=recipient_address,
            subject=subject,
            preview=subject,
            date=_now(),
            is_unread=False,
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

    def _set_sending(self, is_sending: bool) -> None:
        self.send_button.set_sensitive(not is_sending)
        self.from_row.set_sensitive(not is_sending)
        self.cancel_button.set_sensitive(not is_sending)
        self.send_spinner.set_visible(is_sending)
        if is_sending:
            self.send_spinner.start()
        else:
            self.send_spinner.stop()


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _gtk_font() -> tuple[str, str]:
    """Split GTK's "Cantarell 11" style font description into family and size."""
    settings = Gtk.Settings.get_default()
    description = (settings and settings.get_property("gtk-font-name")) or "Sans 11"
    family, _, size = description.rpartition(" ")
    if not size.isdigit():
        return description, "11"
    return family, size


# Build a composer from a mailto: URI. Shared by the main window and by a
# mailto: launch, which opens the composer with no main window at all.
def composer_for_mailto(
    app: Gtk.Application | None,
    db: Database,
    account: Account,
    settings: Gio.Settings,
    uri: str,
) -> PostcardComposerWindow:
    draft = compose.parse_mailto(uri)
    signature = (
        settings.get_string("signature-text").strip()
        if settings.get_boolean("signature-enabled")
        else ""
    )
    return PostcardComposerWindow(
        app,
        db,
        account,
        to=draft.to,
        subject=draft.subject,
        body=draft.body_html
        or (compose.signature_block(signature) if signature else ""),
        cc=draft.cc,
        bcc=draft.bcc,
    )
