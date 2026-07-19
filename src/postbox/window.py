# window.py
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

import gi

gi.require_version("WebKit", "6.0")

import threading
from gettext import gettext as _

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, WebKit

from . import mail_sync
from .account_dialog import PostboxAccountDialog
from .conversation_row import ConversationRow
from .core import secrets
from .core.mime import message_parser
from .core.models.account import Account
from .core.models.attachment import Attachment
from .core.models.email import Email
from .core.models.folder import Folder
from .core.store.database import Database


@Gtk.Template(resource_path="/in/gxanshu/postbox/ui/main-window.ui")
class PostboxMainWindow(Adw.ApplicationWindow):
    __gtype_name__ = "PostboxMainWindow"

    # These fields are filled in automatically from the widgets we named in
    # main-window.blp. The attribute name must match the id in the Blueprint
    # file exactly.
    folder_list: Gtk.ListBox = Gtk.Template.Child()
    conversation_list: Gtk.ListView = Gtk.Template.Child()
    reader_stack: Gtk.Stack = Gtk.Template.Child()
    reader_avatar: Adw.Avatar = Gtk.Template.Child()
    reader_sender: Gtk.Label = Gtk.Template.Child()
    reader_date: Gtk.Label = Gtk.Template.Child()
    reader_subject: Gtk.Label = Gtk.Template.Child()
    reader_body: Gtk.Label = Gtk.Template.Child()
    reader_body_stack: Gtk.Stack = Gtk.Template.Child()
    reader_webview: WebKit.WebView = Gtk.Template.Child()
    images_banner: Adw.Banner = Gtk.Template.Child()
    attachments_group: Gtk.Box = Gtk.Template.Child()
    attachments_list: Gtk.ListBox = Gtk.Template.Child()
    main_stack: Gtk.Stack = Gtk.Template.Child()
    add_account_button: Gtk.Button = Gtk.Template.Child()
    refresh_button: Gtk.Button = Gtk.Template.Child()
    sync_spinner: Gtk.Spinner = Gtk.Template.Child()
    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()

    def __init__(self, app: Gtk.Application, db: Database) -> None:
        super().__init__(application=app)

        self._db: Database = db
        self._current_folder: Folder | None = None
        self._current_email_id: int | None = None
        self._current_html: str | None = None

        self._setup_webview()
        self.images_banner.connect("button-clicked", self._on_show_images_clicked)
        self.add_account_button.connect("clicked", self._on_add_account_clicked)
        self.refresh_button.connect("clicked", self._on_refresh_clicked)

        accounts = self._db.accounts()
        if not accounts:
            self.main_stack.set_visible_child_name("no-account")
            return

        self._load_mail_view(accounts[0])

    def _load_mail_view(self, account: Account) -> None:
        self._account = account
        self._account_id = account.id
        self.main_stack.set_visible_child_name("mail")

        self._folders: Gio.ListStore = Gio.ListStore(item_type=Folder)
        for folder in self._db.folders_for_account(self._account_id):
            self._folders.append(folder)

        self._selection: Gtk.SingleSelection = Gtk.SingleSelection()

        self._load_styles()
        self._setup_folder_sidebar()
        self._setup_conversation_list()

        first = self.folder_list.get_row_at_index(0)
        if first is not None:
            self.folder_list.select_row(first)

    def _on_add_account_clicked(self, _button: Gtk.Button) -> None:
        dialog = PostboxAccountDialog(self._db)
        dialog.connect("account-added", self._on_account_added)
        dialog.present(self)

    def _on_account_added(self, _dialog: PostboxAccountDialog) -> None:
        self._load_mail_view(self._db.accounts()[0])

    # A tiny bit of app CSS: the accent-coloured unread dot and a bold sender
    # name. Loaded from a string so we don't need another resource file yet.
    def _load_styles(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_string(
            ".unread-dot { color: #3584e4; }\n"
            ".conversation-sender { font-weight: bold; }\n"
        )
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    # Remote-content lockdown, set once. Re-applied per message in _show_html
    # so opting in on one email never carries over to the next.
    def _setup_webview(self) -> None:
        settings = self.reader_webview.get_settings()
        settings.set_enable_javascript(False)
        settings.set_auto_load_images(False)

    # The folder list is small and fixed, so a Gtk.ListBox is the simplest
    # tool. bind_model() builds one row per folder and keeps them in sync with
    # the store for free.
    def _setup_folder_sidebar(self) -> None:
        self.folder_list.bind_model(self._folders, self._build_folder_row)
        self.folder_list.connect("row-selected", self._on_folder_selected)

    def _build_folder_row(self, item: GObject.Object) -> Gtk.Widget:
        folder = item
        assert isinstance(folder, Folder)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        box.append(Gtk.Image.new_from_icon_name(folder.icon_name))

        name = Gtk.Label(label=folder.name, xalign=0, hexpand=True)
        box.append(name)

        count = self._db.unread_count_in_folder(folder.id)
        if count > 0:
            badge = Gtk.Label(label=str(count))
            badge.add_css_class("dim-label")
            box.append(badge)

        return box

    def _on_folder_selected(
        self, _list_box: Gtk.ListBox, row: Gtk.ListBoxRow | None
    ) -> None:
        if row is None:
            return

        folder = self._folders.get_item(row.get_index())
        assert isinstance(folder, Folder)
        self._current_folder = folder

        emails = Gio.ListStore(item_type=Email)
        for email in self._db.emails_in_folder(folder.id):
            emails.append(email)

        self._selection.set_model(emails)
        self._selection.unselect_all()
        self.reader_stack.set_visible_child_name("empty")

    # Potentially thousands of rows, so this uses the scalable GTK4 pattern: a
    # GListStore of data, a SingleSelection wrapper, and a factory that recycles
    # a handful of ConversationRow widgets as you scroll.
    def _setup_conversation_list(self) -> None:
        self._selection.set_autoselect(False)
        self._selection.set_can_unselect(True)
        self._selection.connect("selection-changed", self._on_selection_changed)

        self.conversation_list.set_model(self._selection)
        self.conversation_list.set_factory(self._build_conversation_factory())

    # (position, n_items) come from the signal; we just re-read the current
    # selection, so the parameters are ignored.
    def _on_selection_changed(
        self, _selection: Gtk.SingleSelection, _position: int, _n_items: int
    ) -> None:
        self._update_reader()

    def _update_reader(self) -> None:
        email = self._selection.get_selected_item()
        if not isinstance(email, Email):
            self.reader_stack.set_visible_child_name("empty")
            return

        self.reader_avatar.set_text(email.sender)
        self.reader_sender.set_label(email.sender)
        self.reader_date.set_label(email.date)
        self.reader_subject.set_label(email.subject)

        if email.unread:
            email.unread = False
            self._db.mark_email_read(email.id)

        self._open_email(email)

    def _open_email(self, email: Email) -> None:
        self._current_email_id = email.id

        cached = self._db.get_raw_message(email.id)
        if cached is not None:
            self._render_message(email.id, cached)
            return

        self.reader_stack.set_visible_child_name("loading")
        assert self._current_folder is not None
        thread = threading.Thread(
            target=self._fetch_body_worker,
            args=(email, self._current_folder.name),
            daemon=True,
        )
        thread.start()

    # Runs on the worker thread: network only, no Gtk/database access --
    # same rule as _sync_worker below.
    def _fetch_body_worker(self, email: Email, folder_name: str) -> None:
        password = secrets.lookup_password(self._account_id)
        if not password:
            GLib.idle_add(self._on_body_error, email.id, "no saved password")
            return
        try:
            raw = mail_sync.fetch_full_message(
                self._account, password, folder_name, email.server_id
            )
        except Exception as error:
            GLib.idle_add(self._on_body_error, email.id, str(error))
            return
        GLib.idle_add(self._on_body_fetched, email.id, raw)

    # Back on the main thread: safe to touch the database and widgets.
    def _on_body_fetched(self, email_id: int, raw: bytes) -> bool:
        self._db.save_raw_message(email_id, raw)
        if email_id == self._current_email_id:
            self._render_message(email_id, raw)
        return False

    def _on_body_error(self, email_id: int, message: str) -> bool:
        if email_id == self._current_email_id:
            self.reader_stack.set_visible_child_name("message")
            self._toast(_("Couldn't load message: {msg}").format(msg=message))
        return False

    def _render_message(self, email_id: int, raw: bytes) -> None:
        parsed = message_parser.parse_message(raw)

        if parsed.html_body:
            self._show_html(parsed.html_body)
        else:
            self._show_text(parsed.text_body or "")

        self._populate_attachments(parsed.attachments)
        self.reader_stack.set_visible_child_name("message")

    def _show_text(self, text: str) -> None:
        self.images_banner.set_revealed(False)
        self.reader_body.set_label(text)
        self.reader_body_stack.set_visible_child_name("text")

    def _show_html(self, html: str) -> None:
        self._current_html = html
        self.reader_webview.get_settings().set_auto_load_images(False)
        self.images_banner.set_revealed(True)
        self.reader_webview.load_html(html, None)
        self.reader_body_stack.set_visible_child_name("html")

    def _on_show_images_clicked(self, _banner: Adw.Banner) -> None:
        self.reader_webview.get_settings().set_auto_load_images(True)
        self.images_banner.set_revealed(False)
        if self._current_html is not None:
            self.reader_webview.load_html(self._current_html, None)

    def _populate_attachments(self, attachments: list[Attachment]) -> None:
        child = self.attachments_list.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.attachments_list.remove(child)
            child = next_child

        self.attachments_group.set_visible(bool(attachments))
        for attachment in attachments:
            row = Adw.ActionRow(
                title=attachment.filename,
                subtitle=_human_size(attachment.size),
            )
            row.add_prefix(Gtk.Image.new_from_icon_name("mail-attachment-symbolic"))

            save_button = Gtk.Button(
                icon_name="document-save-symbolic", valign=Gtk.Align.CENTER
            )
            save_button.add_css_class("flat")
            save_button.connect(
                "clicked", self._on_save_attachment_clicked, attachment
            )
            row.add_suffix(save_button)

            self.attachments_list.append(row)

    def _on_save_attachment_clicked(
        self, _button: Gtk.Button, attachment: Attachment
    ) -> None:
        dialog = Gtk.FileDialog(initial_name=attachment.filename)
        dialog.save(self, None, self._on_save_dialog_done, attachment)

    def _on_save_dialog_done(
        self,
        dialog: Gtk.FileDialog,
        result: Gio.AsyncResult,
        attachment: Attachment,
    ) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return  # user cancelled the dialog

        file.replace_contents(
            attachment.content, None, False, Gio.FileCreateFlags.NONE, None
        )
        self._toast(_("Saved {name}.").format(name=attachment.filename))

    def _build_conversation_factory(self) -> Gtk.SignalListItemFactory:
        factory = Gtk.SignalListItemFactory()

        # setup: build one empty widget. Runs rarely (only when GTK needs a new
        # reusable row), so it's fine to allocate here.
        def on_setup(_factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
            item.set_child(ConversationRow())

        # bind: fill an existing widget from its item. Runs often (every
        # scroll), so keep it cheap — just copy fields across.
        def on_bind(_factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
            row = item.get_child()
            email = item.get_item()
            assert isinstance(row, ConversationRow)
            assert isinstance(email, Email)
            row.bind(email)

        factory.connect("setup", on_setup)
        factory.connect("bind", on_bind)
        return factory

    def _on_refresh_clicked(self, _button: Gtk.Button) -> None:
        self._start_sync()

    def _start_sync(self) -> None:
        password = secrets.lookup_password(self._account_id)
        if not password:
            self._toast(_("No saved password for this account."))
            return

        self._set_syncing(True)
        thread = threading.Thread(
            target=self._sync_worker,
            args=(self._account, password),
            daemon=True,
        )
        thread.start()

    # Runs on the worker thread: network only, no Gtk/database access.
    def _sync_worker(self, account: Account, password: str) -> None:
        try:
            result = mail_sync.fetch_mailbox(account, password)
        except Exception as error:
            GLib.idle_add(self._on_sync_error, str(error))
            return
        GLib.idle_add(self._on_sync_done, result)

    # Back on the main thread: safe to touch the database and widgets.
    def _on_sync_done(self, result: mail_sync.SyncResult) -> bool:
        for name in result.folders:
            self._db.get_or_create_folder(
                self._account_id, name, mail_sync.icon_for_folder(name)
            )
        inbox = self._db.get_or_create_folder(
            self._account_id, "INBOX", mail_sync.icon_for_folder("INBOX")
        )
        for message in result.messages:
            self._db.save_incoming_email(
                folder_id=inbox.id,
                server_id=message.uid,
                sender=message.sender,
                subject=message.subject,
                preview=message.preview,
                date=message.date,
                unread=message.unread,
            )

        self._reload_folders()
        self._set_syncing(False)
        self._toast(_("Synced {n} messages.").format(n=len(result.messages)))
        return False

    def _on_sync_error(self, message: str) -> bool:
        self._set_syncing(False)
        self._toast(_("Sync failed: {msg}").format(msg=message))
        print("Sync failed:", message)
        return False

    def _set_syncing(self, syncing: bool) -> None:
        self.refresh_button.set_sensitive(not syncing)
        self.sync_spinner.set_visible(syncing)
        if syncing:
            self.sync_spinner.start()
        else:
            self.sync_spinner.stop()

    def _reload_folders(self) -> None:
        selected = self.folder_list.get_selected_row()
        index = selected.get_index() if selected is not None else 0

        self._folders.remove_all()
        for folder in self._db.folders_for_account(self._account_id):
            self._folders.append(folder)

        row = self.folder_list.get_row_at_index(index)
        if row is None:
            row = self.folder_list.get_row_at_index(0)
        if row is not None:
            self.folder_list.select_row(row)

    def _toast(self, text: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=text))


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
