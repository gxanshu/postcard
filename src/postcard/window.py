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

from . import mail_sync
from .account_dialog import PostcardAccountDialog
from .accounts_dialog import PostcardAccountsDialog
from .composer_window import PostcardComposerWindow
from .conversation_row import ConversationRow
from .core import compose, secrets
from .core.models.account import Account
from .core.net import errors
from .core.models.attachment import Attachment
from .core.models.conversation import Conversation
from .core.models.email import Email
from .core.models.folder import Folder
from .core.store.database import Database
from .message_view import MessageView


@Gtk.Template(resource_path="/in/gxanshu/postcard/ui/main-window.ui")
class PostcardMainWindow(Adw.ApplicationWindow):
    __gtype_name__ = "PostcardMainWindow"

    # These fields are filled in automatically from the widgets we named in
    # main-window.blp. The attribute name must match the id in the Blueprint
    # file exactly.
    folder_list: Gtk.ListBox = Gtk.Template.Child()
    conversation_list: Gtk.ListView = Gtk.Template.Child()
    conversation_scroller: Gtk.ScrolledWindow = Gtk.Template.Child()
    conversation_stack: Gtk.Stack = Gtk.Template.Child()
    reader_stack: Gtk.Stack = Gtk.Template.Child()
    reader_subject: Gtk.Label = Gtk.Template.Child()
    thread_box: Gtk.Box = Gtk.Template.Child()
    main_stack: Gtk.Stack = Gtk.Template.Child()
    account_switcher: Gtk.MenuButton = Gtk.Template.Child()
    add_account_button: Gtk.Button = Gtk.Template.Child()
    refresh_button: Gtk.Button = Gtk.Template.Child()
    sync_spinner: Gtk.Spinner = Gtk.Template.Child()
    search_bar: Gtk.SearchBar = Gtk.Template.Child()
    search_entry: Gtk.SearchEntry = Gtk.Template.Child()
    unread_button: Gtk.ToggleButton = Gtk.Template.Child()
    compose_button: Gtk.Button = Gtk.Template.Child()
    reply_button: Gtk.Button = Gtk.Template.Child()
    forward_button: Gtk.Button = Gtk.Template.Child()
    mark_read_button: Gtk.Button = Gtk.Template.Child()
    star_button: Gtk.Button = Gtk.Template.Child()
    move_button: Gtk.MenuButton = Gtk.Template.Child()
    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()
    connection_banner: Adw.Banner = Gtk.Template.Child()

    def __init__(
        self, app: Gtk.Application, db: Database, settings: Gio.Settings
    ) -> None:
        super().__init__(application=app)

        self._db: Database = db
        self._settings: Gio.Settings = settings

        self.set_default_size(
            settings.get_int("window-width"), settings.get_int("window-height")
        )
        if settings.get_boolean("window-maximized"):
            self.maximize()

        self._current_folder: Folder | None = None
        self._active_view: MessageView | None = None
        self._search_timeout: int = 0
        self._rendered_id: int | None = None
        self._suppress_folder_refresh: bool = False
        self._pending_move: dict | None = None
        self._pending_toast: Adw.Toast | None = None

        self._setup_actions()

        self.add_account_button.connect("clicked", self._on_add_account_clicked)
        self.refresh_button.connect("clicked", self._on_refresh_clicked)
        self.compose_button.connect("clicked", self._on_compose_clicked)
        self.reply_button.connect("clicked", self._on_reply_clicked)
        self.forward_button.connect("clicked", self._on_forward_clicked)

        self.search_bar.set_key_capture_widget(self)
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_bar.connect(
            "notify::search-mode-enabled", self._on_search_mode_changed
        )
        self.unread_button.connect("toggled", self._on_unread_toggled)

        # Connected once here (not per account load) so switching accounts
        # doesn't stack duplicate handlers.
        self.folder_list.connect("row-selected", self._on_folder_selected)

        self.connection_banner.connect("button-clicked", self._on_banner_retry)
        self._network = Gio.NetworkMonitor.get_default()
        self._online = self._network.get_network_available()
        self._network_handler = self._network.connect(
            "network-changed", self._on_network_changed
        )

        self._syncing = False
        self._sync_timer_id = 0
        self._interval_handler = self._settings.connect(
            "changed::sync-interval-minutes", lambda *_: self._reschedule_sync()
        )

        self.connect("close-request", self._on_close_request)
        if not self._online:
            self._show_offline_banner()

        accounts = self._db.accounts()
        if not accounts:
            self.main_stack.set_visible_child_name("no-account")
            return

        self._load_mail_view(accounts[0])

    def _load_mail_view(self, account: Account) -> None:
        self._account = account
        self._account_id = account.id

        # Reset per-account reader state so a switch starts clean.
        self._current_folder = None
        self._rendered_id = None
        self._active_view = None
        # Load-on-scroll paging state, keyed by folder id: how many of the
        # newest messages we've paged through (also the next page's offset),
        # and whether older messages remain on the server.
        self._loaded_count: dict[int, int] = {}
        self._has_more: dict[int, bool] = {}
        self.reader_stack.set_visible_child_name("empty")
        self._set_mail_actions_enabled(False)
        self.reply_button.set_sensitive(False)
        self.forward_button.set_sensitive(False)

        self.main_stack.set_visible_child_name("mail")
        self._refresh_account_switcher()

        self._folders: Gio.ListStore = Gio.ListStore(item_type=Folder)
        for folder in self._db.folders_for_account(self._account_id):
            self._folders.append(folder)

        # One persistent store, mutated in place via splice() on every
        # refresh: swapping in a new Gio.ListStore each time (the previous
        # approach) makes GtkListView treat it as a brand new list and reset
        # scroll to the top, which fights load-on-scroll.
        self._conversation_store: Gio.ListStore = Gio.ListStore(item_type=Conversation)
        self._selection: Gtk.SingleSelection = Gtk.SingleSelection(
            model=self._conversation_store
        )

        self._setup_folder_sidebar()
        self._setup_conversation_list()

        # Selecting the inbox kicks off a network fetch for it via
        # _on_folder_selected; the sync below only bootstraps a fresh account
        # whose folders aren't in the database yet.
        self._select_inbox_row()

        self._drain_outbox()
        self._reschedule_sync()
        if self._online:
            self._start_sync(background=True)

    # --- account switcher -------------------------------------------------

    def _refresh_account_switcher(self) -> None:
        self.account_switcher.set_label(self._account.email)
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
            if account.id == self._account_id:
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
        if account.id != self._account_id:
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
            self.main_stack.set_visible_child_name("no-account")
            return
        current = getattr(self, "_account_id", None)
        if current is not None and any(a.id == current for a in accounts):
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
        if self._active_view is None or self._active_view.raw is None:
            return
        headers = email.message_from_bytes(self._active_view.raw, policy=policy.default)
        to_addr = parseaddr(str(headers["From"] or ""))[1]
        subject = compose.reply_subject(str(headers["Subject"] or ""))
        parsed = self._active_view.parsed
        original_text = parsed.text_body if parsed else ""
        body = compose.quote_reply_body(
            str(headers["From"] or ""),
            str(headers["Date"] or ""),
            original_text or "",
            signature=self._signature_text(),
        )
        self._open_composer(to=to_addr, subject=subject, body=body)

    def _on_forward_clicked(self, *_args: object) -> None:
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
            signature=self._signature_text(),
        )
        self._open_composer(subject=subject, body=body)

    def _open_composer(self, to: str = "", subject: str = "", body: str = "") -> None:
        composer = PostcardComposerWindow(
            self.get_application(),
            self._db,
            self._account,
            to=to,
            subject=subject,
            body=body,
        )
        composer.connect("finished", self._on_composer_finished)
        composer.present()

    def _on_composer_finished(self, _composer: PostcardComposerWindow) -> None:
        self._reload_folders()
        self._refresh_conversations()
        self._drain_outbox()

    # --- mail actions -----------------------------------------------------

    def _setup_actions(self) -> None:
        for name, handler in (
            ("toggle-read", self._on_toggle_read),
            ("toggle-star", self._on_toggle_star),
            ("archive", self._on_archive),
            ("trash", self._on_trash),
            ("compose", self._on_compose_clicked),
            ("reply", self._on_reply_clicked),
            ("forward", self._on_forward_clicked),
            ("refresh", self._on_refresh_clicked),
            ("search", self._on_search_action),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

        move = Gio.SimpleAction.new("move", GLib.VariantType.new("s"))
        move.connect("activate", self._on_move)
        self.add_action(move)

        # Flag actions are Ctrl-modified so they don't fire while typing in search.
        app = self.get_application()
        for name, accels in (
            ("win.toggle-read", ["<ctrl>i"]),
            ("win.toggle-star", ["<ctrl>s"]),
            ("win.archive", ["<ctrl>e"]),
            ("win.trash", ["<ctrl>Delete"]),
            ("win.compose", ["<ctrl>n"]),
            ("win.reply", ["<ctrl>r"]),
            ("win.forward", ["<ctrl><shift>f"]),
            ("win.refresh", ["F5"]),
            ("win.search", ["<ctrl>f"]),
        ):
            app.set_accels_for_action(name, accels)

    def _set_mail_actions_enabled(self, enabled: bool) -> None:
        for name in ("toggle-read", "toggle-star", "archive", "trash", "move"):
            action = self.lookup_action(name)
            if action is not None:
                action.set_enabled(enabled)
        self.move_button.set_sensitive(enabled)

    def _on_toggle_read(self, _action: Gio.SimpleAction, _param: object) -> None:
        conversation = self._selection.get_selected_item()
        if not isinstance(conversation, Conversation):
            return

        unread = not conversation.unread
        for mail in conversation.emails:
            mail.unread = unread
            if unread:
                self._db.mark_email_unread(mail.id)
            else:
                self._db.mark_email_read(mail.id)

        def revert() -> None:
            for mail in conversation.emails:
                mail.unread = not unread
                if unread:
                    self._db.mark_email_read(mail.id)
                else:
                    self._db.mark_email_unread(mail.id)
            self._after_flag_change(conversation)

        self._after_flag_change(conversation)
        uids = [mail.server_id for mail in conversation.emails]
        self._run_flag_worker(uids, "\\Seen", add=not unread, revert=revert)

    def _on_toggle_star(self, _action: Gio.SimpleAction, _param: object) -> None:
        conversation = self._selection.get_selected_item()
        if not isinstance(conversation, Conversation):
            return

        starred = not conversation.starred
        for mail in conversation.emails:
            mail.starred = starred
            self._db.set_email_starred(mail.id, starred)

        def revert() -> None:
            for mail in conversation.emails:
                mail.starred = not starred
                self._db.set_email_starred(mail.id, not starred)
            self._after_flag_change(conversation)

        self._after_flag_change(conversation)
        uids = [mail.server_id for mail in conversation.emails]
        self._run_flag_worker(uids, "\\Flagged", add=starred, revert=revert)

    # Clear the unread flag for a whole conversation: locally, in the badges
    # and list, and on the server. A no-op if it's already read, so reopening a
    # read thread costs nothing.
    def _mark_conversation_read(self, conversation: Conversation) -> None:
        if not conversation.unread:
            return

        for mail in conversation.emails:
            mail.unread = False
            self._db.mark_email_read(mail.id)

        def revert() -> None:
            for mail in conversation.emails:
                mail.unread = True
                self._db.mark_email_unread(mail.id)
            self._after_flag_change(conversation)

        self._after_flag_change(conversation)
        uids = [mail.server_id for mail in conversation.emails]
        self._run_flag_worker(uids, "\\Seen", add=True, revert=revert)

    # Update badges and the list after a flag change, keeping this
    # conversation selected (so the reader doesn't reload).
    def _after_flag_change(self, conversation: Conversation) -> None:
        self._reload_folders()
        self._refresh_conversations(keep_id=conversation.id)

    def _run_flag_worker(
        self, uids: list[str], flag: str, add: bool, revert: Callable[[], None]
    ) -> None:
        password = secrets.lookup_password(self._account_id)
        folder = self._current_folder
        if not password or folder is None:
            return
        thread = threading.Thread(
            target=self._flag_worker,
            args=(self._account, password, folder.name, uids, flag, add, revert),
            daemon=True,
        )
        thread.start()

    # Runs on the worker thread: network only, no Gtk/database access.
    def _flag_worker(
        self,
        account: Account,
        password: str,
        folder_name: str,
        uids: list[str],
        flag: str,
        add: bool,
        revert: Callable[[], None],
    ) -> None:
        try:
            mail_sync.set_flag(account, password, folder_name, uids, flag, add)
        except Exception as error:
            GLib.idle_add(self._on_action_failed, revert, str(error))

    def _on_action_failed(self, revert: Callable[[], None], message: str) -> bool:
        revert()
        self._toast(_("Action failed: {msg}").format(msg=message))
        return False

    # --- archive / trash / move (with undo) -------------------------------

    def _on_archive(self, _action: Gio.SimpleAction, _param: object) -> None:
        self._start_move_by_role("archive", _("Archived"))

    def _on_trash(self, _action: Gio.SimpleAction, _param: object) -> None:
        self._start_move_by_role("trash", _("Deleted"))

    def _on_move(self, _action: Gio.SimpleAction, param: GLib.Variant) -> None:
        conversation = self._selection.get_selected_item()
        if not isinstance(conversation, Conversation):
            return
        dest = self._find_folder_by_name(param.get_string())
        if dest is not None:
            self._start_move(
                conversation, dest, _("Moved to {name}").format(name=dest.name)
            )

    def _start_move_by_role(self, role: str, verb: str) -> None:
        conversation = self._selection.get_selected_item()
        if not isinstance(conversation, Conversation):
            return
        dest = self._folder_with_role(role)
        if dest is None:
            self._toast(_("No {role} folder found.").format(role=role))
            return
        self._start_move(conversation, dest, verb)

    def _folder_with_role(self, role: str) -> Folder | None:
        current_id = self._current_folder.id if self._current_folder else None
        for folder in self._db.folders_for_account(self._account_id):
            if (
                folder.id != current_id
                and mail_sync.role_for_folder(folder.name) == role
            ):
                return folder
        return None

    def _find_folder_by_name(self, name: str) -> Folder | None:
        for folder in self._db.folders_for_account(self._account_id):
            if folder.name == name:
                return folder
        return None

    # Move a conversation optimistically: update the DB and drop it from the
    # list now, then run the real IMAP MOVE ~5s later. An Undo toast cancels the
    # server move if clicked before then.
    def _start_move(self, conversation: Conversation, dest: Folder, verb: str) -> None:
        source = self._current_folder
        if source is None or dest.id == source.id:
            return

        self._commit_pending_move()

        email_ids = [mail.id for mail in conversation.emails]
        uids = [mail.server_id for mail in conversation.emails]
        for email_id in email_ids:
            self._db.move_email(email_id, dest.id)

        self._reload_folders()
        self._refresh_conversations()

        toast = Adw.Toast(title=verb, button_label=_("Undo"))
        toast.connect("button-clicked", self._on_undo_move)
        self._pending_toast = toast
        self._pending_move = {
            "email_ids": email_ids,
            "uids": uids,
            "source": source,
            "dest": dest,
            "timeout_id": GLib.timeout_add(5000, self._on_move_timeout),
        }
        self.toast_overlay.add_toast(toast)

    def _on_undo_move(self, _toast: Adw.Toast) -> None:
        pending = self._pending_move
        if pending is None:
            return
        GLib.source_remove(pending["timeout_id"])
        self._pending_move = None
        self._pending_toast = None
        self._restore_move(pending)

    # The undo window elapsed — actually send the move to the server.
    def _on_move_timeout(self) -> bool:
        pending = self._pending_move
        self._pending_move = None
        self._pending_toast = None
        if pending is not None:
            self._run_move_worker(pending)
        return False

    # A newer action arrived: send the previous pending move now instead of
    # waiting for its timer.
    def _commit_pending_move(self) -> None:
        pending = self._pending_move
        if pending is None:
            return
        GLib.source_remove(pending["timeout_id"])
        self._pending_move = None
        if self._pending_toast is not None:
            self._pending_toast.dismiss()
            self._pending_toast = None
        self._run_move_worker(pending)

    def _restore_move(self, pending: dict) -> None:
        for email_id in pending["email_ids"]:
            self._db.move_email(email_id, pending["source"].id)
        self._reload_folders()
        self._refresh_conversations()

    def _run_move_worker(self, pending: dict) -> None:
        password = secrets.lookup_password(self._account_id)
        if not password:
            return
        thread = threading.Thread(
            target=self._move_worker,
            args=(self._account, password, pending),
            daemon=True,
        )
        thread.start()

    # Runs on the worker thread: network only, no Gtk/database access.
    def _move_worker(self, account: Account, password: str, pending: dict) -> None:
        try:
            mail_sync.move_messages(
                account,
                password,
                pending["source"].name,
                pending["uids"],
                pending["dest"].name,
            )
        except Exception as error:
            GLib.idle_add(self._on_move_failed, pending, str(error))

    def _on_move_failed(self, pending: dict, message: str) -> bool:
        self._restore_move(pending)
        self._toast(_("Move failed: {msg}").format(msg=message))
        return False

    # A menu of every folder except the current one, each targeting win.move.
    def _build_move_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        current_id = self._current_folder.id if self._current_folder else None
        for folder in self._db.folders_for_account(self._account_id):
            if folder.id == current_id:
                continue
            label = mail_sync.display_name_for_folder(folder.name)
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value(
                "win.move", GLib.Variant.new_string(folder.name)
            )
            menu.append_item(item)
        return menu

    # Select the right-clicked row, then pop up its actions menu.
    def _on_row_right_click(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
        item: Gtk.ListItem,
    ) -> None:
        position = item.get_position()
        if position == Gtk.INVALID_LIST_POSITION:
            return
        self._selection.set_selected(position)

        conversation = self._selection.get_selected_item()
        if not isinstance(conversation, Conversation):
            return

        popover = Gtk.PopoverMenu.new_from_model(self._context_menu(conversation))
        popover.set_parent(gesture.get_widget())
        popover.set_has_arrow(False)
        popover.connect("closed", lambda p: p.unparent())

        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.popup()

    def _context_menu(self, conversation: Conversation) -> Gio.Menu:
        menu = Gio.Menu()

        flags = Gio.Menu()
        read = _("Mark Unread") if not conversation.unread else _("Mark Read")
        star = _("Unstar") if conversation.starred else _("Star")
        flags.append(read, "win.toggle-read")
        flags.append(star, "win.toggle-star")
        menu.append_section(None, flags)

        actions = Gio.Menu()
        actions.append(_("Archive"), "win.archive")
        actions.append(_("Delete"), "win.trash")
        actions.append_submenu(_("Move to"), self._build_move_menu())
        menu.append_section(None, actions)

        return menu

    # The folder list is small and fixed, so a Gtk.ListBox is the simplest
    # tool. bind_model() builds one row per folder and keeps them in sync with
    # the store for free.
    def _setup_folder_sidebar(self) -> None:
        self.folder_list.bind_model(self._folders, self._build_folder_row)

    def _build_folder_row(self, item: GObject.Object) -> Gtk.Widget:
        folder = item
        assert isinstance(folder, Folder)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        box.append(Gtk.Image.new_from_icon_name(folder.icon_name))

        name = Gtk.Label(
            label=mail_sync.display_name_for_folder(folder.name),
            xalign=0,
            hexpand=True,
        )
        box.append(name)

        count = self._db.unread_count_in_folder(folder.id)
        if count > 0:
            badge = Gtk.Label(label=str(count))
            badge.add_css_class("dim-label")
            box.append(badge)

        return box

    # Select the inbox row (or the first folder if we can't spot one).
    def _select_inbox_row(self) -> None:
        target = 0
        for i in range(self._folders.get_n_items()):
            folder = self._folders.get_item(i)
            assert isinstance(folder, Folder)
            if mail_sync.role_for_folder(folder.name) == "inbox":
                target = i
                break
        row = self.folder_list.get_row_at_index(target)
        if row is not None:
            self.folder_list.select_row(row)

    def _on_folder_selected(
        self, _list_box: Gtk.ListBox, row: Gtk.ListBoxRow | None
    ) -> None:
        if row is None:
            return

        folder = self._folders.get_item(row.get_index())
        assert isinstance(folder, Folder)
        previous = self._current_folder
        self._current_folder = folder
        self.move_button.set_menu_model(self._build_move_menu())
        if self._suppress_folder_refresh:
            return
        self._refresh_conversations()
        # Show cached mail instantly, then pull this folder from the server.
        # Guard on a real folder change: rebuilding the sidebar re-emits
        # row-selected for the same folder (sometimes after the suppress flag is
        # cleared), which would otherwise sync in a tight loop.
        changed = previous is None or previous.id != folder.id
        if changed and self._online:
            self._start_sync(background=True, folder_name=folder.name)

    # Rebuild the conversation list from the current folder, applying the
    # search query if one is typed. Called on folder change and search change.
    # keep_id re-selects that conversation if it's still in the list, so a mail
    # action can refresh without reloading the reader.
    def _refresh_conversations(self, keep_id: int | None = None) -> None:
        if self._current_folder is None:
            return

        query = self.search_entry.get_text().strip()
        if query:
            matches = self._db.search_conversations(self._current_folder.id, query)
        else:
            matches = self._db.conversations_in_folder(self._current_folder.id)

        if self.unread_button.get_active():
            # Keep the conversation being read (keep_id) even once it's marked
            # read, so opening a mail here doesn't make it vanish under you; it
            # drops out on the next refresh when you move to another.
            matches = [c for c in matches if c.unread or c.id == keep_id]

        # Update the existing store's contents rather than swapping in a new
        # one, so the ListView keeps its scroll position.
        store = self._conversation_store
        store.splice(0, store.get_n_items(), matches)

        if store.get_n_items() > 0:
            self.conversation_stack.set_visible_child_name("list")
        elif self._syncing:
            self.conversation_stack.set_visible_child_name("loading")
        else:
            self.conversation_stack.set_visible_child_name("empty")

        target = -1
        if keep_id is not None:
            for index in range(store.get_n_items()):
                if store.get_item(index).id == keep_id:
                    target = index
                    break

        if target >= 0:
            self._selection.set_selected(target)
        else:
            self._selection.unselect_all()
            self._update_reader()

    # Debounce keystrokes: query the database ~200ms after typing stops instead
    # of on every letter.
    def _on_search_changed(self, _entry: Gtk.SearchEntry) -> None:
        if self._search_timeout:
            GLib.source_remove(self._search_timeout)
        self._search_timeout = GLib.timeout_add(200, self._on_search_timeout)

    def _on_search_timeout(self) -> bool:
        self._search_timeout = 0
        self._refresh_conversations()
        return False

    # Closing the search bar clears the query so the full list comes back.
    def _on_search_mode_changed(
        self, search_bar: Gtk.SearchBar, _param: GObject.ParamSpec
    ) -> None:
        if not search_bar.get_search_mode():
            self.search_entry.set_text("")

    def _on_unread_toggled(self, _button: Gtk.ToggleButton) -> None:
        self._refresh_conversations()

    # Potentially thousands of rows, so this uses the scalable GTK4 pattern: a
    # GListStore of data, a SingleSelection wrapper, and a factory that recycles
    # a handful of ConversationRow widgets as you scroll.
    def _setup_conversation_list(self) -> None:
        self._selection.set_autoselect(False)
        self._selection.set_can_unselect(True)
        self._selection.connect("selection-changed", self._on_selection_changed)

        self.conversation_list.set_model(self._selection)
        self.conversation_list.set_factory(self._build_conversation_factory())

        # Load older mail when the list is scrolled to the bottom.
        self.conversation_scroller.connect("edge-reached", self._on_list_edge_reached)

    # Scrolling to the bottom pulls the next-older page for the current folder,
    # if the last sync said there's more to fetch.
    def _on_list_edge_reached(
        self, _scroller: Gtk.ScrolledWindow, pos: Gtk.PositionType
    ) -> None:
        if pos != Gtk.PositionType.BOTTOM:
            return
        folder = self._current_folder
        if folder is None or self._syncing or not self._online:
            return
        if not self._has_more.get(folder.id, False):
            return
        self._start_sync(
            background=True,
            folder_name=folder.name,
            offset=self._loaded_count.get(folder.id, 0),
        )

    # (position, n_items) come from the signal; we just re-read the current
    # selection, so the parameters are ignored.
    def _on_selection_changed(
        self, _selection: Gtk.SingleSelection, _position: int, _n_items: int
    ) -> None:
        self._update_reader()

    def _update_reader(self) -> None:
        conversation = self._selection.get_selected_item()
        if not isinstance(conversation, Conversation):
            self._rendered_id = None
            self.reply_button.set_sensitive(False)
            self.forward_button.set_sensitive(False)
            self._set_mail_actions_enabled(False)
            self.reader_stack.set_visible_child_name("empty")
            return

        self._update_action_buttons(conversation)

        # Already showing this thread (e.g. after a flag change) — don't rebuild.
        if conversation.id == self._rendered_id:
            self.reader_stack.set_visible_child_name("message")
            return

        self._rendered_id = conversation.id
        self.reply_button.set_sensitive(False)
        self.forward_button.set_sensitive(False)
        self._active_view = None
        self._render_thread(conversation)
        # Opening a conversation marks it read (like most mail clients).
        self._mark_conversation_read(conversation)

    # Reflect the selected conversation's state on the action buttons.
    def _update_action_buttons(self, conversation: Conversation) -> None:
        self._set_mail_actions_enabled(True)

        if conversation.unread:
            self.mark_read_button.set_icon_name("mail-read-symbolic")
            self.mark_read_button.set_tooltip_text(_("Mark Read"))
        else:
            self.mark_read_button.set_icon_name("mail-unread-symbolic")
            self.mark_read_button.set_tooltip_text(_("Mark Unread"))

        if conversation.starred:
            self.star_button.set_icon_name("starred-symbolic")
            self.star_button.set_tooltip_text(_("Unstar"))
        else:
            self.star_button.set_icon_name("non-starred-symbolic")
            self.star_button.set_tooltip_text(_("Star"))

    # Build one MessageView per email, newest first. The newest starts expanded
    # (which loads its body); older ones load lazily when the user expands them.
    def _render_thread(self, conversation: Conversation) -> None:
        self.reader_subject.set_label(conversation.subject)

        child = self.thread_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.thread_box.remove(child)
            child = next_child

        remote_images = self._settings.get_boolean("load-remote-images")
        emails = list(reversed(conversation.emails))
        for index, mail in enumerate(emails):
            newest = index == 0
            view = MessageView(
                mail,
                on_load=self._load_body,
                on_save_attachment=self._save_attachment,
                on_rendered=self._on_newest_rendered if newest else None,
                expanded=newest,
                remote_images=remote_images,
            )
            self.thread_box.append(view)

        self.reader_stack.set_visible_child_name("message")

    def _on_newest_rendered(self, _view: MessageView) -> None:
        self._active_view = _view
        self.reply_button.set_sensitive(True)
        self.forward_button.set_sensitive(True)

    # Fetch one message's raw bytes for a MessageView: serve the cached copy if
    # we have it, else pull it over IMAP on a worker thread. (Marking read is
    # handled once per conversation in _mark_conversation_read.)
    def _load_body(self, mail: Email, callback: Callable) -> None:
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
    def _body_worker(self, mail: Email, folder_name: str, callback: Callable) -> None:
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
        # reusable row), so it's fine to allocate here. A right-click gesture
        # opens the actions menu for that row.
        def on_setup(_factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
            row = ConversationRow()
            gesture = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
            gesture.connect("pressed", self._on_row_right_click, item)
            row.add_controller(gesture)
            item.set_child(row)

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

    def _on_refresh_clicked(self, *_args: object) -> None:
        self._drain_outbox()
        self._start_sync()

    def _on_search_action(self, _action: Gio.SimpleAction, _param: object) -> None:
        self.search_bar.set_search_mode(not self.search_bar.get_search_mode())

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
            items.append((item.id, item.subject, compose.extract_recipients(raw), raw))
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
        for email_id, subject, recipients, raw in items:
            try:
                mail_sync.send_message(
                    account, password, account.email, recipients, raw
                )
                results.append((email_id, subject, raw, True))
            except Exception:
                results.append((email_id, subject, raw, False))
        GLib.idle_add(self._on_outbox_drained, results)

    # Back on the main thread: safe to touch the database and widgets.
    def _on_outbox_drained(self, results: list[tuple[int, str, bytes, bool]]) -> bool:
        sent_folder: Folder | None = None
        sent_count = 0
        for email_id, subject, raw, ok in results:
            if not ok:
                continue
            if sent_folder is None:
                sent_folder = self._db.get_or_create_folder(
                    self._account_id, "Sent", mail_sync.icon_for_folder("Sent")
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
            self._refresh_conversations()
            self._toast(_("Sent {n} queued message(s).").format(n=sent_count))
        return False

    def _start_sync(
        self,
        background: bool = False,
        folder_name: str | None = None,
        offset: int = 0,
    ) -> None:
        # Don't pile background syncs (folder clicks, the poll timer) on top of
        # one already running.
        if background and self._syncing:
            return
        password = secrets.lookup_password(self._account_id)
        if not password:
            if not background:
                self._toast(_("No saved password for this account."))
            return

        self._set_syncing(True)
        if self.conversation_stack.get_visible_child_name() == "empty":
            self.conversation_stack.set_visible_child_name("loading")
        thread = threading.Thread(
            target=self._sync_worker,
            args=(self._account, password, folder_name, offset),
            daemon=True,
        )
        thread.start()

    # Refresh on a timer using the configured interval (0 = manual only).
    def _reschedule_sync(self) -> None:
        if self._sync_timer_id:
            GLib.source_remove(self._sync_timer_id)
            self._sync_timer_id = 0
        minutes = self._settings.get_int("sync-interval-minutes")
        if minutes > 0:
            self._sync_timer_id = GLib.timeout_add_seconds(
                minutes * 60, self._on_sync_tick
            )

    def _on_sync_tick(self) -> bool:
        if (
            getattr(self, "_account_id", None) is not None
            and self._online
            and not self._syncing
        ):
            self._drain_outbox()
            self._start_sync(background=True)
        return True

    # Runs on the worker thread: network only, no Gtk/database access.
    def _sync_worker(
        self,
        account: Account,
        password: str,
        folder_name: str | None,
        offset: int = 0,
    ) -> None:
        try:
            result = mail_sync.fetch_mailbox(
                account, password, folder_name, offset=offset
            )
        except Exception as error:
            category, message = errors.classify(error, account.imap_host)
            GLib.idle_add(self._on_sync_error, category, message)
            return
        GLib.idle_add(self._on_sync_done, result)

    # Back on the main thread: safe to touch the database and widgets.
    def _on_sync_done(self, result: mail_sync.SyncResult) -> bool:
        # Remember the open conversation so a background poll doesn't yank it.
        selected = self._selection.get_selected_item()
        keep_id = selected.id if isinstance(selected, Conversation) else None

        for name in result.folders:
            self._db.get_or_create_folder(
                self._account_id, name, mail_sync.icon_for_folder(name)
            )
        # Mirror the server's folder list, keeping only the local Outbox. This
        # clears stale rows like a duplicate "INBOX" from earlier versions.
        if result.folders:
            self._db.prune_folders(self._account_id, set(result.folders) | {"Outbox"})

        target = self._db.get_or_create_folder(
            self._account_id, result.folder, mail_sync.icon_for_folder(result.folder)
        )
        new_messages: list[mail_sync.MessageHeader] = []
        for message in result.messages:
            added = self._db.save_incoming_email(
                folder_id=target.id,
                server_id=message.uid,
                sender=message.sender,
                subject=message.subject,
                preview=message.preview,
                date=message.date,
                unread=message.unread,
                starred=message.starred,
                message_id=message.message_id,
                in_reply_to=message.in_reply_to,
                references=message.references,
            )
            if added and message.unread:
                new_messages.append(message)
        self._db.reassign_conversations(target.id)

        # Update paging state: track the deepest page loaded (max() so a
        # newest-page poll never forgets how far the user has scrolled back),
        # and offer "more" only while messages remain beyond it.
        reached = result.offset + len(result.messages)
        loaded = min(result.exists, max(self._loaded_count.get(target.id, 0), reached))
        self._loaded_count[target.id] = loaded
        self._has_more[target.id] = result.exists > loaded

        self._set_syncing(False)
        self._reload_folders()
        self._refresh_conversations(keep_id=keep_id)
        self.connection_banner.set_revealed(False)

        # Only nag about new mail when the user isn't already looking.
        if new_messages and not self.is_active():
            self._notify_new_mail(new_messages)
        return False

    def _notify_new_mail(self, messages: list[mail_sync.MessageHeader]) -> None:
        if not self._settings.get_boolean("notifications"):
            return
        app = self.get_application()
        if app is None:
            return

        if len(messages) == 1:
            notification = Gio.Notification.new(messages[0].sender)
            notification.set_body(messages[0].subject)
        else:
            senders = ", ".join(dict.fromkeys(m.sender for m in messages))
            notification = Gio.Notification.new(
                _("{n} new messages").format(n=len(messages))
            )
            notification.set_body(senders)

        notification.set_default_action("app.focus-mail")
        app.send_notification("new-mail", notification)

    def _on_sync_error(self, category: str, message: str) -> bool:
        self._set_syncing(False)
        # Auth failures aren't worth a Retry button (same password); everything
        # else is a transient connection problem the user can retry.
        button = "" if category == "auth" else _("Retry")
        self._show_connection_banner(message, button)
        return False

    # --- connection banner / offline handling -----------------------------

    def _show_connection_banner(self, title: str, button_label: str = "") -> None:
        self.connection_banner.set_title(title)
        self.connection_banner.set_button_label(button_label)
        self.connection_banner.set_revealed(True)

    def _show_offline_banner(self) -> None:
        self._show_connection_banner(
            _("You're offline. Postcard will reconnect when your connection returns.")
        )

    def _on_banner_retry(self, _banner: Adw.Banner) -> None:
        self.connection_banner.set_revealed(False)
        if getattr(self, "_account_id", None) is not None:
            self._drain_outbox()
            self._start_sync()

    # network-changed fires on any change; act only on real online/offline flips.
    def _on_network_changed(
        self, _monitor: Gio.NetworkMonitor, available: bool
    ) -> None:
        if available == self._online:
            return
        self._online = available
        if not available:
            self._show_offline_banner()
            return
        self.connection_banner.set_revealed(False)
        if getattr(self, "_account_id", None) is not None:
            self._drain_outbox()
            self._start_sync()

    def _on_close_request(self, _window: Gtk.Window) -> bool:
        width, height = self.get_default_size()
        self._settings.set_int("window-width", width)
        self._settings.set_int("window-height", height)
        self._settings.set_boolean("window-maximized", self.is_maximized())

        if self._settings.get_boolean("run-in-background"):
            self.set_visible(False)
            self._notify_background()

            # Keep the app alive so the sync timer keeps running.
            return True

        self._network.disconnect(self._network_handler)
        self._settings.disconnect(self._interval_handler)
        if self._sync_timer_id:
            GLib.source_remove(self._sync_timer_id)

        return False

    def _notify_background(self) -> None:
        if getattr(self, "_bg_notified", False):
            return

        self._bg_notified = True

        app = self.get_application()
        if app is None:
            return

        n = Gio.Notification.new(_("Postcard is running in the background"))
        n.set_body(_("It will keep checking for new mail. Quit to stop."))
        n.set_default_action("app.focus-mail")
        app.send_notification("running-background", n)

    def _set_syncing(self, syncing: bool) -> None:
        self._syncing = syncing
        self.refresh_button.set_sensitive(not syncing)
        self.sync_spinner.set_visible(syncing)
        if syncing:
            self.sync_spinner.start()
        else:
            self.sync_spinner.stop()

    # Rebuild the folder sidebar (refreshes the unread badges). Re-selecting the
    # row is suppressed so it doesn't rebuild the conversation list — callers
    # that want that refresh it explicitly.
    def _reload_folders(self) -> None:
        # Preserve the selection by folder id, not row index — pruning stale
        # folders can shift the indices.
        keep_id = self._current_folder.id if self._current_folder else None

        self._suppress_folder_refresh = True
        self._folders.remove_all()
        target = 0
        for i, folder in enumerate(self._db.folders_for_account(self._account_id)):
            self._folders.append(folder)
            if folder.id == keep_id:
                target = i

        row = self.folder_list.get_row_at_index(target)
        if row is not None:
            self.folder_list.select_row(row)
        self._suppress_folder_refresh = False

    def _toast(self, text: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=text))
