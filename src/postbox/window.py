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

import email
import threading
from collections.abc import Callable
from datetime import datetime
from email import policy
from email.utils import parseaddr
from gettext import gettext as _

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from . import mail_send, mail_sync
from .account_dialog import PostboxAccountDialog
from .composer_window import PostboxComposerWindow
from .conversation_row import ConversationRow
from .core import compose, secrets
from .core.models.account import Account
from .core.models.attachment import Attachment
from .core.models.conversation import Conversation
from .core.models.email import Email
from .core.models.folder import Folder
from .core.store.database import Database
from .message_view import MessageView


@Gtk.Template(resource_path="/in/gxanshu/postbox/ui/main-window.ui")
class PostboxMainWindow(Adw.ApplicationWindow):
    __gtype_name__ = "PostboxMainWindow"

    # These fields are filled in automatically from the widgets we named in
    # main-window.blp. The attribute name must match the id in the Blueprint
    # file exactly.
    folder_list: Gtk.ListBox = Gtk.Template.Child()
    conversation_list: Gtk.ListView = Gtk.Template.Child()
    reader_stack: Gtk.Stack = Gtk.Template.Child()
    reader_subject: Gtk.Label = Gtk.Template.Child()
    thread_box: Gtk.Box = Gtk.Template.Child()
    main_stack: Gtk.Stack = Gtk.Template.Child()
    add_account_button: Gtk.Button = Gtk.Template.Child()
    refresh_button: Gtk.Button = Gtk.Template.Child()
    sync_spinner: Gtk.Spinner = Gtk.Template.Child()
    compose_button: Gtk.Button = Gtk.Template.Child()
    reply_button: Gtk.Button = Gtk.Template.Child()
    forward_button: Gtk.Button = Gtk.Template.Child()
    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()

    def __init__(self, app: Gtk.Application, db: Database) -> None:
        super().__init__(application=app)

        self._db: Database = db
        self._current_folder: Folder | None = None
        self._active_view: MessageView | None = None

        self.add_account_button.connect("clicked", self._on_add_account_clicked)
        self.refresh_button.connect("clicked", self._on_refresh_clicked)
        self.compose_button.connect("clicked", self._on_compose_clicked)
        self.reply_button.connect("clicked", self._on_reply_clicked)
        self.forward_button.connect("clicked", self._on_forward_clicked)

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

        self._drain_outbox()

    def _on_add_account_clicked(self, _button: Gtk.Button) -> None:
        dialog = PostboxAccountDialog(self._db)
        dialog.connect("account-added", self._on_account_added)
        dialog.present(self)

    def _on_account_added(self, _dialog: PostboxAccountDialog) -> None:
        self._load_mail_view(self._db.accounts()[0])

    def _on_compose_clicked(self, _button: Gtk.Button) -> None:
        self._open_composer()

    def _on_reply_clicked(self, _button: Gtk.Button) -> None:
        if self._active_view is None or self._active_view.raw is None:
            return
        headers = email.message_from_bytes(self._active_view.raw, policy=policy.default)
        to_addr = parseaddr(str(headers["From"] or ""))[1]
        subject = compose.reply_subject(str(headers["Subject"] or ""))
        parsed = self._active_view.parsed
        original_text = parsed.text_body if parsed else ""
        body = compose.quote_reply_body(
            str(headers["From"] or ""), str(headers["Date"] or ""), original_text or ""
        )
        self._open_composer(to=to_addr, subject=subject, body=body)

    def _on_forward_clicked(self, _button: Gtk.Button) -> None:
        if self._active_view is None or self._active_view.raw is None:
            return
        headers = email.message_from_bytes(self._active_view.raw, policy=policy.default)
        subject = compose.forward_subject(str(headers["Subject"] or ""))
        parsed = self._active_view.parsed
        original_text = parsed.text_body if parsed else ""
        body = compose.forward_body(
            str(headers["From"] or ""),
            str(headers["Date"] or ""),
            str(headers["Subject"] or ""),
            original_text or "",
        )
        self._open_composer(subject=subject, body=body)

    def _open_composer(self, to: str = "", subject: str = "", body: str = "") -> None:
        composer = PostboxComposerWindow(
            self.get_application(),
            self._db,
            self._account,
            to=to,
            subject=subject,
            body=body,
        )
        composer.connect("finished", self._on_composer_finished)
        composer.present()

    def _on_composer_finished(self, _composer: PostboxComposerWindow) -> None:
        self._reload_folders()
        self._drain_outbox()

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

        conversations = Gio.ListStore(item_type=Conversation)
        for conversation in self._db.conversations_in_folder(folder.id):
            conversations.append(conversation)

        self._selection.set_model(conversations)
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
        self.reply_button.set_sensitive(False)
        self.forward_button.set_sensitive(False)

        conversation = self._selection.get_selected_item()
        if not isinstance(conversation, Conversation):
            self.reader_stack.set_visible_child_name("empty")
            return

        self._active_view = None
        self._render_thread(conversation)

    # Build one MessageView per email, oldest first. The newest starts expanded
    # (which loads its body); older ones load lazily when the user expands them.
    def _render_thread(self, conversation: Conversation) -> None:
        self.reader_subject.set_label(conversation.subject)

        child = self.thread_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.thread_box.remove(child)
            child = next_child

        emails = conversation.emails
        last = len(emails) - 1
        for index, mail in enumerate(emails):
            newest = index == last
            view = MessageView(
                mail,
                on_load=self._load_body,
                on_save_attachment=self._save_attachment,
                on_rendered=self._on_newest_rendered if newest else None,
                expanded=newest,
            )
            self.thread_box.append(view)

        self.reader_stack.set_visible_child_name("message")

    def _on_newest_rendered(self, _view: MessageView) -> None:
        self._active_view = _view
        self.reply_button.set_sensitive(True)
        self.forward_button.set_sensitive(True)

    # Fetch one message's raw bytes for a MessageView: mark it read, serve the
    # cached copy if we have it, else pull it over IMAP on a worker thread.
    def _load_body(self, mail: Email, callback: Callable) -> None:
        if mail.unread:
            mail.unread = False
            self._db.mark_email_read(mail.id)

        cached = self._db.get_raw_message(mail.id)
        if cached is not None:
            callback(cached, None)
            return

        assert self._current_folder is not None
        thread = threading.Thread(
            target=self._body_worker,
            args=(mail, self._current_folder.name, callback),
            daemon=True,
        )
        thread.start()

    # Runs on the worker thread: network only, no Gtk/database access.
    def _body_worker(
        self, mail: Email, folder_name: str, callback: Callable
    ) -> None:
        password = secrets.lookup_password(self._account_id)
        if not password:
            GLib.idle_add(
                self._deliver_body, callback, mail.id, None, "no saved password"
            )
            return
        try:
            raw = mail_sync.fetch_full_message(
                self._account, password, folder_name, mail.server_id
            )
        except Exception as error:
            GLib.idle_add(self._deliver_body, callback, mail.id, None, str(error))
            return
        GLib.idle_add(self._deliver_body, callback, mail.id, raw, None)

    # Back on the main thread: cache the body, then hand it to the MessageView.
    def _deliver_body(
        self,
        callback: Callable,
        email_id: int,
        raw: bytes | None,
        error: str | None,
    ) -> bool:
        if raw is not None:
            self._db.save_raw_message(email_id, raw)
        callback(raw, error)
        return False

    def _save_attachment(self, attachment: Attachment) -> None:
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
            conversation = item.get_item()
            assert isinstance(row, ConversationRow)
            assert isinstance(conversation, Conversation)
            row.bind(conversation)

        factory.connect("setup", on_setup)
        factory.connect("bind", on_bind)
        return factory

    def _on_refresh_clicked(self, _button: Gtk.Button) -> None:
        self._drain_outbox()
        self._start_sync()

    def _drain_outbox(self) -> None:
        outbox = next(
            (
                folder
                for folder in self._db.folders_for_account(self._account_id)
                if folder.name == "Outbox"
            ),
            None,
        )
        if outbox is None:
            return

        pending = self._db.emails_in_folder(outbox.id)
        if not pending:
            return

        password = secrets.lookup_password(self._account_id)
        if not password:
            return

        items = []
        for item in pending:
            raw = self._db.get_raw_message(item.id)
            if raw is None:
                continue
            items.append((item.id, item.subject, compose.extract_to_addresses(raw), raw))
        if not items:
            return

        thread = threading.Thread(
            target=self._outbox_worker,
            args=(self._account, password, items),
            daemon=True,
        )
        thread.start()

    # Runs on the worker thread: network only, no Gtk/database access.
    def _outbox_worker(
        self,
        account: Account,
        password: str,
        items: list[tuple[int, str, list[str], bytes]],
    ) -> None:
        results = []
        for email_id, subject, to_addrs, raw in items:
            try:
                mail_send.send_message(account, password, account.email, to_addrs, raw)
                results.append((email_id, subject, raw, True))
            except Exception:
                results.append((email_id, subject, raw, False))
        GLib.idle_add(self._on_outbox_drained, results)

    # Back on the main thread: safe to touch the database and widgets.
    def _on_outbox_drained(
        self, results: list[tuple[int, str, bytes, bool]]
    ) -> bool:
        sent_folder: Folder | None = None
        sent_count = 0
        for email_id, subject, raw, ok in results:
            if not ok:
                continue
            if sent_folder is None:
                sent_folder = self._db.get_or_create_folder(
                    self._account_id, "Sent", "mail-sent-symbolic"
                )
            row = self._db.save_email(
                sent_folder.id,
                sender=self._account.email,
                subject=subject,
                preview=subject,
                date=datetime.now().strftime("%b %d"),
                unread=False,
            )
            self._db.save_raw_message(row.id, raw)
            self._db.delete_email(email_id)
            sent_count += 1

        if sent_count:
            self._reload_folders()
            self._toast(_("Sent {n} queued message(s).").format(n=sent_count))
        return False

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
                message_id=message.message_id,
                in_reply_to=message.in_reply_to,
                references=message.references,
            )
        self._db.reassign_conversations(inbox.id)

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
