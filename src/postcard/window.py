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
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.utils import parseaddr
from gettext import gettext as _
from gettext import ngettext

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from . import mail_sync
from .account_dialog import PostcardAccountDialog
from .accounts_dialog import PostcardAccountsDialog
from .avatar_loader import AvatarLoader
from .composer_window import PostcardComposerWindow
from .conversation_row import ConversationRow
from .core import compose, secrets
from .core.mime.message_parser import ParsedMessage
from .core.models.account import Account
from .core.models.attachment import Attachment
from .core.models.conversation import Conversation
from .core.models.email import Email
from .core.models.folder import Folder
from .core.net import errors
from .core.store.database import Database
from .folder_row import FolderRow
from .message_view import MessageView

# Window action names, grouped by what enables and disables them together.
MAIL_ACTIONS = ("toggle-read", "toggle-star", "archive", "trash", "move")
REPLY_FORWARD_ACTIONS = ("reply", "forward")

# Local-only queue for mail composed while offline; never mirrored from the
# server, so prune_folders keeps it. "Sent" is matched on the server list.
OUTBOX_FOLDER = "Outbox"
SENT_FOLDER = "Sent"


@dataclass(frozen=True, slots=True)
class OutboxResult:
    """One attempted send from the Outbox. error is None when it went out."""

    email_id: int
    subject: str
    raw: bytes
    error: Exception | None


@Gtk.Template(resource_path="/in/gxanshu/postcard/ui/main-window.ui")
class PostcardMainWindow(Adw.ApplicationWindow):
    __gtype_name__ = "PostcardMainWindow"

    # These fields are filled in automatically from the widgets we named in
    # main-window.blp. The attribute name must match the id in the Blueprint
    # file exactly.
    folder_list: Gtk.ListView = Gtk.Template.Child()
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

        # None until _load_mail_view runs, which __init__ skips entirely when
        # there are no accounts yet. Read it through a guard clause, never
        # directly -- background callbacks (sync timer, network-changed,
        # notification actions) can fire while it is still None.
        self._account: Account | None = None

        self._current_folder: Folder | None = None
        self._active_view: MessageView | None = None
        self._search_timeout: int = 0
        self._rendered_id: int | None = None
        self._suppress_folder_refresh: bool = False
        self._selection_update_in_progress: bool = False
        self._pending_move: dict | None = None
        self._pending_toast: Adw.Toast | None = None
        # Source UIDs stay protected while an optimistic move is pending or
        # its worker is in flight.  Completed moves remain protected until a
        # newest-page sync confirms that the source UID is gone.  The two
        # counts keep overlapping moves from clearing one another's tombstones.
        self._move_tombstones: dict[tuple[int, str], dict[str, int]] = {}

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

        self.connection_banner.connect("button-clicked", self._on_banner_retry)
        self._network = Gio.NetworkMonitor.get_default()
        self._online = self._network.get_network_available()
        self._network_handler = self._network.connect(
            "network-changed", self._on_network_changed
        )

        self._avatars = AvatarLoader(self._settings)
        self._avatar_handler = self._settings.connect(
            "changed::load-sender-avatars", lambda *_: self._refresh_conversations()
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
        self._set_reply_forward_enabled(False)

        self.main_stack.set_visible_child_name("mail")
        self._refresh_account_switcher()

        # Boxes of the rows currently on screen, keyed by folder id, so
        # _reload_folders can refresh them without rebuilding the tree.
        self._folder_rows: dict[int, FolderRow] = {}
        # The (id, parent_id) pairs the tree was last built from.
        self._folder_shape: list[tuple[int, int | None]] = []
        # Folders grouped by parent id; None holds the roots.
        self._folder_children: dict[int | None, list[Folder]] = {}

        self._folder_root_store: Gio.ListStore = Gio.ListStore(item_type=Folder)
        self._folder_tree_model: Gtk.TreeListModel = Gtk.TreeListModel.new(
            self._folder_root_store,
            False,
            True,
            self._folder_children_func,
            None,
            None,
        )
        self._folder_selection: Gtk.SingleSelection = Gtk.SingleSelection(
            model=self._folder_tree_model
        )
        self._folder_selection.connect("selection-changed", self._on_folder_selected)

        # One persistent store, mutated in place via splice() on every
        # refresh: swapping in a new Gio.ListStore each time (the previous
        # approach) makes GtkListView treat it as a brand new list and reset
        # scroll to the top, which fights load-on-scroll.
        self._conversation_store: Gio.ListStore = Gio.ListStore(item_type=Conversation)
        self._selection: Gtk.MultiSelection = Gtk.MultiSelection(
            model=self._conversation_store
        )

        self._setup_folder_sidebar()
        self._setup_conversation_list()

        self._reload_folders()

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
            self.main_stack.set_visible_child_name("no-account")
            return
        current = self._account.id if self._account else None
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
        if (
            len(self._selected_conversations()) != 1
            or self._active_view is None
            or self._active_view.raw is None
        ):
            return
        headers = email.message_from_bytes(self._active_view.raw, policy=policy.default)
        to_addr = parseaddr(str(headers["From"] or ""))[1]
        subject = compose.reply_subject(str(headers["Subject"] or ""))
        parsed = self._active_view.parsed
        body = compose.quote_reply_body(
            str(headers["From"] or ""),
            str(headers["Date"] or ""),
            _original_text(parsed),
            signature=self._signature_text(),
        )
        self._open_composer(to=to_addr, subject=subject, body=body)

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

    def _open_composer(self, to: str = "", subject: str = "", body: str = "") -> None:
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
        if app is not None:
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

    def _set_actions_enabled(self, names: tuple[str, ...], enabled: bool) -> None:
        # Only Gio.SimpleAction can be toggled; the plain Gio.Action interface
        # exposes no setter, so anything else is skipped rather than crashing.
        for name in names:
            action = self.lookup_action(name)
            if isinstance(action, Gio.SimpleAction):
                action.set_enabled(enabled)

    def _set_mail_actions_enabled(self, enabled: bool) -> None:
        self._set_actions_enabled(MAIL_ACTIONS, enabled)
        self.move_button.set_sensitive(enabled)

    def _set_reply_forward_enabled(self, enabled: bool) -> None:
        self._set_actions_enabled(REPLY_FORWARD_ACTIONS, enabled)

    def _selected_conversations(self) -> list[Conversation]:
        selected = []
        for position in range(self._conversation_store.get_n_items()):
            if not self._selection.is_selected(position):
                continue
            conversation = self._conversation_store.get_item(position)
            if isinstance(conversation, Conversation):
                selected.append(conversation)
        return selected

    def _selected_conversation(self) -> Conversation | None:
        selected = self._selected_conversations()
        return selected[0] if len(selected) == 1 else None

    def _on_toggle_read(self, _action: Gio.SimpleAction, _param: object) -> None:
        conversations = self._selected_conversations()
        if not conversations:
            return
        self._toggle_read_many(conversations)

    def _toggle_read(self, conversation: Conversation) -> None:
        self._toggle_read_many([conversation])

    def _toggle_read_many(self, conversations: list[Conversation]) -> None:
        # A mixed selection follows the aggregate command shown in the menu:
        # if anything is unread, mark the whole selection read.
        unread = not any(conversation.unread for conversation in conversations)
        originals = {
            mail.id: mail.unread
            for conversation in conversations
            for mail in conversation.emails
        }
        for conversation in conversations:
            for mail in conversation.emails:
                mail.unread = unread
                if unread:
                    self._db.mark_email_unread(mail.id)
                else:
                    self._db.mark_email_read(mail.id)

        def revert() -> None:
            for conversation in conversations:
                for mail in conversation.emails:
                    old_unread = originals[mail.id]
                    mail.unread = old_unread
                    if old_unread:
                        self._db.mark_email_unread(mail.id)
                    else:
                        self._db.mark_email_read(mail.id)
            self._after_flag_change(conversations)

        self._after_flag_change(conversations)
        uids = [
            uid
            for conversation in conversations
            for uid in mail_sync.server_uids(conversation)
        ]
        self._run_flag_worker(uids, "\\Seen", add=not unread, revert=revert)

    def _on_toggle_star(self, _action: Gio.SimpleAction, _param: object) -> None:
        conversations = self._selected_conversations()
        if not conversations:
            return
        self._toggle_star_many(conversations)

    def _toggle_star(self, conversation: Conversation) -> None:
        self._toggle_star_many([conversation])

    def _toggle_star_many(self, conversations: list[Conversation]) -> None:
        # As with read state, one aggregate command gives the whole selection a
        # deterministic state even when the conversations are mixed.
        starred = not any(conversation.starred for conversation in conversations)
        originals = {
            mail.id: mail.starred
            for conversation in conversations
            for mail in conversation.emails
        }
        for conversation in conversations:
            for mail in conversation.emails:
                mail.starred = starred
                self._db.set_email_starred(mail.id, starred)

        def revert() -> None:
            for conversation in conversations:
                for mail in conversation.emails:
                    old_starred = originals[mail.id]
                    mail.starred = old_starred
                    self._db.set_email_starred(mail.id, old_starred)
            self._after_flag_change(conversations)

        self._after_flag_change(conversations)
        uids = [
            uid
            for conversation in conversations
            for uid in mail_sync.server_uids(conversation)
        ]
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
        uids = mail_sync.server_uids(conversation)
        self._run_flag_worker(uids, "\\Seen", add=True, revert=revert)

    # Update badges and the list after a flag change, keeping this
    # conversation selected (so the reader doesn't reload).
    def _after_flag_change(
        self, conversations: Conversation | list[Conversation]
    ) -> None:
        if isinstance(conversations, Conversation):
            keep_id = conversations.id
        else:
            keep_id = conversations[0].id if len(conversations) == 1 else None
        self._reload_folders()
        self._refresh_conversations(keep_id=keep_id)

    def _run_flag_worker(
        self, uids: list[str], flag: str, add: bool, revert: Callable[[], None]
    ) -> None:
        account = self._account
        folder = self._current_folder
        if account is None or folder is None:
            return
        password = secrets.lookup_password(account.id)
        if not password:
            return
        thread = threading.Thread(
            target=self._flag_worker,
            args=(account, password, folder.name, uids, flag, add, revert),
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
        self._start_move_by_role("archive")

    def _on_trash(self, _action: Gio.SimpleAction, _param: object) -> None:
        self._start_move_by_role("trash")

    def _on_move(self, _action: Gio.SimpleAction, param: GLib.Variant) -> None:
        conversations = self._selected_conversations()
        if not conversations:
            return
        self._move_to(conversations[0], param)

    def _move_to(self, conversation: Conversation, param: GLib.Variant) -> None:
        dest = self._find_folder_by_name(param.get_string())
        if dest is not None:
            conversations = self._selected_conversations() or [conversation]
            count = len(conversations)
            title = ngettext(
                "Moved to {name}",
                "Moved {n} conversations to {name}",
                count,
            ).format(n=count, name=dest.name)
            self._start_move(conversations, dest, title)

    def _start_move_by_role(
        self, role: str, conversation: Conversation | None = None
    ) -> None:
        conversations = self._selected_conversations()
        if not conversations and conversation is not None:
            conversations = [conversation]
        if not conversations:
            return
        dest = self._folder_with_role(role)
        if dest is None:
            self._toast(_("No {role} folder found.").format(role=role))
            return
        count = len(conversations)
        if role == "archive":
            title = ngettext("Archived", "Archived {n} conversations", count).format(
                n=count
            )
        else:
            title = ngettext("Deleted", "Deleted {n} conversations", count).format(
                n=count
            )
        self._start_move(conversations, dest, title)

    def _folder_with_role(self, role: str) -> Folder | None:
        if self._account is None:
            return None
        current_id = self._current_folder.id if self._current_folder else None
        matches = []
        for folder in self._db.folders_for_account(self._account.id):
            if (
                folder.id != current_id
                and mail_sync.role_for_folder(folder.name) == role
            ):
                matches.append(folder)
        if role == "archive":
            for folder in matches:
                if "archive" in folder.name.lower():
                    return folder
        return matches[0] if matches else None

    def _find_folder_by_name(self, name: str) -> Folder | None:
        if self._account is None:
            return None
        for folder in self._db.folders_for_account(self._account.id):
            if folder.name == name:
                return folder
        return None

    # Move a conversation optimistically: update the DB and drop it from the
    # list now, then run the real IMAP MOVE ~5s later. An Undo toast cancels the
    # server move if clicked before then.
    def _start_move(
        self, conversations: list[Conversation], dest: Folder, verb: str
    ) -> None:
        source = self._current_folder
        if source is None or dest.id == source.id:
            return

        self._commit_pending_move()

        # Pair each mail with its UID in one pass so the "has a UID" narrowing
        # survives into email_ids/uids/originals, which stay index-aligned.
        # A locally saved copy has no UID yet -- see mail_sync.server_uids.
        mails_with_uids = [
            (mail, mail.server_id)
            for conversation in conversations
            for mail in conversation.emails
            if mail.server_id is not None
        ]
        email_ids = [mail.id for mail, _ in mails_with_uids]
        uids = [uid for _, uid in mails_with_uids]
        originals = [(mail.id, source.id, uid) for mail, uid in mails_with_uids]
        tombstones = [(source.id, uid) for uid in uids]
        self._db.move_emails(email_ids, dest.id)
        for tombstone in tombstones:
            state = self._move_tombstones.setdefault(
                tombstone, {"active": 0, "awaiting": 0}
            )
            state["active"] += 1

        self._reload_folders()
        self._refresh_conversations()

        toast = Adw.Toast(title=verb, button_label=_("Undo"))
        toast.connect("button-clicked", self._on_undo_move)
        self._pending_toast = toast
        self._pending_move = {
            "email_ids": email_ids,
            "uids": uids,
            "originals": originals,
            "source": source,
            "dest": dest,
            "tombstones": tombstones,
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
        self._db.restore_emails(pending["originals"])
        self._clear_move_tombstones(pending)
        self._reload_folders()
        self._refresh_conversations()

    def _clear_move_tombstones(self, pending: dict, start: int = 0) -> None:
        for tombstone in pending["tombstones"][start:]:
            state = self._move_tombstones.get(tombstone)
            if state is None:
                continue
            state["active"] -= 1
            if state["active"] <= 0 and state["awaiting"] <= 0:
                self._move_tombstones.pop(tombstone, None)

    def _await_move_tombstones(self, pending: dict, completed: int) -> None:
        for tombstone in pending["tombstones"][:completed]:
            state = self._move_tombstones.get(tombstone)
            if state is None:
                continue
            state["active"] -= 1
            state["awaiting"] += 1

    def _confirm_move_tombstones(self, folder_id: int, all_uids: set[str]) -> None:
        for (tombstone_folder_id, uid), state in list(self._move_tombstones.items()):
            if tombstone_folder_id != folder_id or uid in all_uids:
                continue
            state["awaiting"] = 0
            if state["active"] <= 0:
                self._move_tombstones.pop((tombstone_folder_id, uid), None)

    def _run_move_worker(self, pending: dict) -> None:
        account = self._account
        password = secrets.lookup_password(account.id) if account else None
        if account is None or not password:
            self._restore_move(pending)
            self._toast(_("No saved password for this account."))
            return
        thread = threading.Thread(
            target=self._move_worker,
            args=(account, password, pending),
            daemon=True,
        )
        thread.start()

    # Runs on the worker thread: network only, no Gtk/database access.
    def _move_worker(self, account: Account, password: str, pending: dict) -> None:
        try:
            result = mail_sync.move_messages(
                account,
                password,
                pending["source"].name,
                pending["uids"],
                pending["dest"].name,
            )
            GLib.idle_add(self._on_move_result, pending, result)
        except Exception as error:
            GLib.idle_add(self._on_move_failed, pending, str(error))

    def _on_move_result(self, pending: dict, result: mail_sync.MoveResult) -> bool:
        completed = len(result.destination_uids)
        self._await_move_tombstones(pending, completed)
        completed_moves = list(
            zip(
                pending["email_ids"][:completed],
                [pending["dest"].id] * completed,
                result.destination_uids,
                strict=True,
            )
        )
        if completed_moves:
            self._db.reconcile_moved_emails(completed_moves)
        failed_index = result.failed_index
        if failed_index is None and completed < len(pending["uids"]):
            failed_index = completed
        if failed_index is not None:
            self._db.restore_emails(pending["originals"][failed_index:])
            self._clear_move_tombstones(pending, failed_index)
        self._reload_folders()
        self._refresh_conversations()
        if result.error is not None:
            self._toast(_("Move failed: {msg}").format(msg=result.error))
        return False

    def _on_move_failed(self, pending: dict, message: str) -> bool:
        self._restore_move(pending)
        self._toast(_("Move failed: {msg}").format(msg=message))
        return False

    def _rebuild_move_menu(self) -> None:
        self.move_button.set_menu_model(self._build_move_menu())

    # A menu of every folder except the current one, each targeting the given
    # action prefix (the toolbar uses win.move and context menus use context.move).
    def _build_move_menu(self, action_prefix: str = "win") -> Gio.Menu:
        menu = Gio.Menu()
        if self._account is None:
            return menu
        current_id = self._current_folder.id if self._current_folder else None
        for folder in self._db.folders_for_account(self._account.id):
            if folder.id == current_id:
                continue
            label = mail_sync.display_name_for_folder(
                folder.name, folder.display_delimiter
            )
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value(
                f"{action_prefix}.move", GLib.Variant.new_string(folder.name)
            )
            menu.append_item(item)
        return menu

    # Select an unselected right-clicked row, then pop up its actions menu.
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

        if not self._selection.is_selected(position):
            self._selection_update_in_progress = True
            try:
                self._selection.unselect_all()
                self._selection.select_item(position, True)
            finally:
                self._selection_update_in_progress = False
            self._update_reader()

        conversation = self._conversation_store.get_item(position)
        if not isinstance(conversation, Conversation):
            return

        # A gesture detached from its widget has nothing to anchor the popover
        # to; that can't happen while the row is on screen, but set_parent(None)
        # would abort in C rather than raise, so bail out here instead.
        row_widget = gesture.get_widget()
        if row_widget is None:
            return

        popover = Gtk.PopoverMenu()
        popover.insert_action_group("context", self._context_actions(conversation))
        popover.set_parent(row_widget)
        popover.set_menu_model(self._context_menu(conversation))
        popover.set_has_arrow(False)
        # GtkModelButton activates its action after closing the popover, so keep
        # the action hierarchy alive until activation has finished.
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))

        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.popup()

    def _context_actions(self, _conversation: Conversation) -> Gio.SimpleActionGroup:
        """Create actions for the current selection and its context menu row."""
        actions = Gio.SimpleActionGroup()

        toggle_read = Gio.SimpleAction.new("toggle-read", None)
        toggle_read.connect("activate", self._on_toggle_read)
        actions.add_action(toggle_read)

        toggle_star = Gio.SimpleAction.new("toggle-star", None)
        toggle_star.connect("activate", self._on_toggle_star)
        actions.add_action(toggle_star)

        archive = Gio.SimpleAction.new("archive", None)
        archive.connect("activate", self._on_archive)
        actions.add_action(archive)

        trash = Gio.SimpleAction.new("trash", None)
        trash.connect("activate", self._on_trash)
        actions.add_action(trash)

        move = Gio.SimpleAction.new("move", GLib.VariantType.new("s"))
        move.connect("activate", self._on_move)
        actions.add_action(move)

        return actions

    def _context_menu(self, conversation: Conversation) -> Gio.Menu:
        menu = Gio.Menu()

        selected = self._selected_conversations() or [conversation]

        flags = Gio.Menu()
        read = _("Mark Read") if any(c.unread for c in selected) else _("Mark Unread")
        star = _("Unstar") if any(c.starred for c in selected) else _("Star")
        flags.append(read, "context.toggle-read")
        flags.append(star, "context.toggle-star")
        menu.append_section(None, flags)

        actions = Gio.Menu()
        actions.append(_("Archive"), "context.archive")
        actions.append(_("Delete"), "context.trash")
        actions.append_submenu(_("Move to"), self._build_move_menu("context"))
        menu.append_section(None, actions)

        return menu

    def _folder_children_func(
        self, item: GObject.Object, *_args: object
    ) -> Gio.ListStore | None:
        assert isinstance(item, Folder)
        children = self._folder_children.get(item.id)
        if not children:
            return None
        store = Gio.ListStore(item_type=Folder)
        for child in children:
            store.append(child)
        return store

    def _setup_folder_sidebar(self) -> None:
        self.folder_list.set_model(self._folder_selection)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_folder_row_setup)
        factory.connect("bind", self._on_folder_row_bind)
        factory.connect("unbind", self._on_folder_row_unbind)
        self.folder_list.set_factory(factory)

    # setup: build one empty widget, reused for many folders as the list
    # scrolls. The expander draws the indent and the expand/collapse arrow.
    def _on_folder_row_setup(
        self, _factory: Gtk.SignalListItemFactory, item: Gtk.ListItem
    ) -> None:
        expander = Gtk.TreeExpander()
        expander.set_child(FolderRow())
        item.set_child(expander)

    # bind: fill an existing widget from its item. Runs on every scroll, so it
    # only copies fields across.
    def _on_folder_row_bind(
        self, _factory: Gtk.SignalListItemFactory, item: Gtk.ListItem
    ) -> None:
        expander = item.get_child()
        assert isinstance(expander, Gtk.TreeExpander)

        tree_list_row = item.get_item()
        assert isinstance(tree_list_row, Gtk.TreeListRow)
        expander.set_list_row(tree_list_row)

        folder = tree_list_row.get_item()
        assert isinstance(folder, Folder)

        row = expander.get_child()
        assert isinstance(row, FolderRow)

        self._folder_rows[folder.id] = row
        row.bind(folder, self._db.unread_count_in_folder(folder.id))

    def _on_folder_row_unbind(
        self, _factory: Gtk.SignalListItemFactory, item: Gtk.ListItem
    ) -> None:
        tree_list_row = item.get_item()
        if isinstance(tree_list_row, Gtk.TreeListRow):
            folder = tree_list_row.get_item()
            if isinstance(folder, Folder):
                self._folder_rows.pop(folder.id, None)

        expander = item.get_child()
        if isinstance(expander, Gtk.TreeExpander):
            expander.set_list_row(None)

    # Select the inbox row, or the first folder if we can't spot one.
    def _select_inbox_row(self) -> None:
        n = self._folder_tree_model.get_n_items()
        if n == 0:
            return
        target = 0
        for i in range(n):
            tree_row = self._folder_tree_model.get_item(i)
            if isinstance(tree_row, Gtk.TreeListRow):
                folder = tree_row.get_item()
                assert isinstance(folder, Folder)
                if mail_sync.role_for_folder(folder.name) == "inbox":
                    target = i
                    break

        # Row 0 is autoselected when the tree is built, so set_selected() may
        # emit nothing. Load the folder directly instead.
        self._suppress_folder_refresh = True
        self._folder_selection.set_selected(target)
        self._suppress_folder_refresh = False
        self._current_folder = None
        self._on_folder_selected(self._folder_selection, target, 1)

    def _on_folder_selected(
        self,
        selection: Gtk.SingleSelection,
        _position: int,
        _n_items: int,
    ) -> None:
        tree_list_row = selection.get_selected_item()
        if not isinstance(tree_list_row, Gtk.TreeListRow):
            return

        folder = tree_list_row.get_item()
        assert isinstance(folder, Folder)
        previous = self._current_folder
        self._current_folder = folder
        self.move_button.set_menu_model(self._build_move_menu())
        if self._suppress_folder_refresh:
            return
        self._refresh_conversations()
        # Only sync on a real folder change — rebuilding the sidebar re-emits
        # selection-changed for the same folder, which would loop.
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

        vadj = self.conversation_scroller.get_vadjustment()
        scroll_pos = vadj.get_value() if vadj else 0.0

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

        # Clear the selection before updating the existing store. MultiSelection
        # tracks positions, while keep_id tracks the conversation itself. Hold
        # the reader update until the store and reselection are both complete.
        store = self._conversation_store
        target = -1
        if keep_id is not None:
            for index, conversation in enumerate(matches):
                if conversation.id == keep_id:
                    target = index
                    break

        self._selection_update_in_progress = True
        try:
            self._selection.unselect_all()
            store.splice(0, store.get_n_items(), matches)

            if target >= 0:
                self._selection.select_item(target, True)
            else:
                self._selection.unselect_all()
        finally:
            self._selection_update_in_progress = False

        if store.get_n_items() > 0:
            self.conversation_stack.set_visible_child_name("list")
        elif self._syncing:
            self.conversation_stack.set_visible_child_name("loading")
        else:
            self.conversation_stack.set_visible_child_name("empty")

        self._update_reader()

        if vadj is not None and scroll_pos > 0:
            GLib.idle_add(
                lambda: (
                    vadj.set_value(
                        min(scroll_pos, vadj.get_upper() - vadj.get_page_size())
                    )
                    or False
                )
            )

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
    # GListStore of data, a MultiSelection wrapper, and a factory that recycles
    # a handful of ConversationRow widgets as you scroll.
    def _setup_conversation_list(self) -> None:
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
        self, _selection: Gtk.MultiSelection, _position: int, _n_items: int
    ) -> None:
        if self._selection_update_in_progress:
            return
        self._update_reader()

    def _update_reader(self) -> None:
        selected = self._selected_conversations()
        if len(selected) != 1:
            self._rendered_id = None
            self._active_view = None
            self.reply_button.set_sensitive(False)
            self.forward_button.set_sensitive(False)
            self._set_reply_forward_enabled(False)
            self._set_mail_actions_enabled(bool(selected))
            if selected:
                self._update_action_buttons(selected)
            self.reader_stack.set_visible_child_name("empty")
            return

        conversation = selected[0]
        self._update_action_buttons(selected)
        self._set_reply_forward_enabled(False)

        # Already showing this thread (e.g. after a flag change) — don't rebuild.
        if conversation.id == self._rendered_id:
            ready = self._active_view is not None and self._active_view.raw is not None
            self.reply_button.set_sensitive(ready)
            self.forward_button.set_sensitive(ready)
            self._set_reply_forward_enabled(ready)
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
    def _update_action_buttons(
        self, conversations: Conversation | list[Conversation]
    ) -> None:
        if isinstance(conversations, Conversation):
            selected = [conversations]
        else:
            selected = conversations
        self._set_mail_actions_enabled(True)

        if any(conversation.unread for conversation in selected):
            self.mark_read_button.set_icon_name("mail-read-symbolic")
            self.mark_read_button.set_tooltip_text(_("Mark Read"))
        else:
            self.mark_read_button.set_icon_name("mail-unread-symbolic")
            self.mark_read_button.set_tooltip_text(_("Mark Unread"))

        if any(conversation.starred for conversation in selected):
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
                avatars=self._avatars,
            )
            self.thread_box.append(view)

        self.reader_stack.set_visible_child_name("message")

    def _on_newest_rendered(self, _view: MessageView) -> None:
        if len(self._selected_conversations()) != 1:
            return
        self._active_view = _view
        self.reply_button.set_sensitive(True)
        self.forward_button.set_sensitive(True)
        self._set_reply_forward_enabled(True)

    # Fetch one message's raw bytes for a MessageView: serve the cached copy if
    # we have it, else pull it over IMAP on a worker thread. (Marking read is
    # handled once per conversation in _mark_conversation_read.)
    def _load_body(self, mail: Email, callback: Callable) -> None:
        cached = self._db.get_raw_message(mail.id)
        if cached is not None:
            callback(cached, None)
            return

        if not mail.server_id:
            # No UID and no cached copy: nothing to fetch until the next sync.
            callback(None, _("This message hasn't finished syncing yet."))
            return

        account = self._account
        folder = self._current_folder
        if account is None or folder is None:
            callback(None, _("No account is open."))
            return

        password = secrets.lookup_password(account.id)
        if not password:
            callback(None, _("No saved password for this account."))
            return

        thread = threading.Thread(
            target=self._body_worker,
            args=(mail.id, mail.server_id, folder.name, callback, account, password),
            daemon=True,
        )
        thread.start()

    # Runs on the worker thread: network only, no Gtk/database access. Takes
    # plain values rather than the Email, which the main thread may mutate --
    # the account and password are resolved by _load_body for the same reason.
    def _body_worker(
        self,
        email_id: int,
        uid: str,
        folder_name: str,
        callback: Callable,
        account: Account,
        password: str,
    ) -> None:
        try:
            raw = mail_sync.fetch_full_message(account, password, folder_name, uid)
        except Exception as error:
            GLib.idle_add(self._deliver_body, callback, email_id, None, str(error))
            return
        GLib.idle_add(self._deliver_body, callback, email_id, raw, None)

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
            row = ConversationRow(self._avatars)
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
        account = self._account
        if account is None:
            return

        outbox = next(
            (
                folder
                for folder in self._db.folders_for_account(account.id)
                if folder.name == OUTBOX_FOLDER
            ),
            None,
        )
        if outbox is None:
            return

        pending = self._db.emails_in_folder(outbox.id)
        if not pending:
            return

        password = secrets.lookup_password(account.id)
        if not password:
            return

        jobs = []
        for mail in pending:
            raw = self._db.get_raw_message(mail.id)
            if raw is None:
                continue
            jobs.append((mail.id, mail.subject, compose.extract_recipients(raw), raw))
        if not jobs:
            return

        thread = threading.Thread(
            target=self._outbox_worker,
            args=(account, password, jobs),
            daemon=True,
        )
        thread.start()

    # Runs on the worker thread: network only, no Gtk/database access. Failures
    # travel back as the exception itself -- the mail stays in the Outbox, so
    # the user has to be told why rather than left believing it was sent.
    def _outbox_worker(
        self,
        account: Account,
        password: str,
        jobs: list[tuple[int, str, list[str], bytes]],
    ) -> None:
        results: list[OutboxResult] = []
        for email_id, subject, recipients, raw in jobs:
            try:
                mail_sync.send_message(
                    account, password, account.email, recipients, raw
                )
            except Exception as error:  # noqa: BLE001 - reported, not swallowed
                results.append(OutboxResult(email_id, subject, raw, error))
            else:
                results.append(OutboxResult(email_id, subject, raw, None))
        GLib.idle_add(self._on_outbox_drained, results)

    # Back on the main thread: safe to touch the database and widgets.
    def _on_outbox_drained(self, results: list[OutboxResult]) -> bool:
        account = self._account
        if account is None:
            return False

        sent_folder: Folder | None = None
        sent_count = 0
        for result in results:
            if result.error is not None:
                continue
            if sent_folder is None:
                sent_folder = self._db.get_or_create_folder(
                    account.id, SENT_FOLDER, mail_sync.icon_for_folder(SENT_FOLDER)
                )
            row = self._db.save_email(
                sent_folder.id,
                sender=account.email,
                sender_address=account.email,
                subject=result.subject,
                preview=result.subject,
                date=datetime.now().strftime("%b %d"),
                unread=False,
            )
            self._db.save_raw_message(row.id, result.raw)
            self._db.delete_email(result.email_id)
            sent_count += 1

        if sent_count:
            self._reload_folders()
            self._refresh_conversations()
            self._toast(_("Sent {n} queued message(s).").format(n=sent_count))

        # Queued mail that could not be sent is still in the Outbox, so say so
        # -- silently leaving it there reads as "sent" to the user.
        send_errors = [result.error for result in results if result.error is not None]
        if send_errors:
            category, message = errors.classify(send_errors[0], account.smtp_host)
            self._show_connection_banner(
                ngettext(
                    "Couldn't send a queued message. {reason}",
                    "Couldn't send {n} queued messages. {reason}",
                    len(send_errors),
                ).format(n=len(send_errors), reason=message),
                self._retry_button_label(category),
            )
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
        account = self._account
        password = secrets.lookup_password(account.id) if account else None
        if account is None or not password:
            if not background:
                self._toast(_("No saved password for this account."))
            return

        self._set_syncing(True)
        if self.conversation_stack.get_visible_child_name() == "empty":
            self.conversation_stack.set_visible_child_name("loading")
        thread = threading.Thread(
            target=self._sync_worker,
            args=(account, password, folder_name, offset),
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
        if self._account is not None and self._online and not self._syncing:
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
        account = self._account
        if account is None:
            return False

        # Remember the open conversation so a background poll doesn't yank it.
        selected = self._selected_conversation()
        keep_id = selected.id if selected is not None else None

        mailboxes = [
            m for m in result.folders if m.name not in mail_sync.NAMESPACE_ROOTS
        ]

        # Shortest name first: a parent's name is a prefix of its children's, so
        # every parent is stored before a child looks it up.
        for mailbox in sorted(mailboxes, key=lambda m: len(m.name)):
            name, delimiter = mailbox.name, mailbox.delimiter
            selectable = "\\Noselect" not in mailbox.flags
            icon = mail_sync.icon_for_folder(name) if selectable else "folder-symbolic"
            folder = self._db.get_or_create_folder(account.id, name, icon)

            parent_name = mail_sync.parent_mailbox_name(name, delimiter)
            parent = self._db.get_folder_by_name(account.id, parent_name)
            self._db.set_folder_parent(
                folder.id, parent.id if parent else None, delimiter
            )

        if mailboxes:
            # Mirror the server's folder list, keeping only the local Outbox.
            # This clears stale rows like a duplicate "INBOX" from earlier
            # versions.
            names = {m.name for m in mailboxes} | {OUTBOX_FOLDER}
            self._db.prune_folders(account.id, names)

        target = self._db.get_or_create_folder(
            account.id, result.folder, mail_sync.icon_for_folder(result.folder)
        )
        new_messages: list[mail_sync.MessageHeader] = []
        for message in result.messages:
            if (target.id, message.uid) in self._move_tombstones:
                continue
            added = self._db.save_incoming_email(
                folder_id=target.id,
                server_id=message.uid,
                sender=message.sender,
                sender_address=message.sender_address,
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
        if result.all_uids is not None:
            self._db.prune_stale_emails(target.id, result.all_uids)
            # Do this after filtering the fetched headers: an older snapshot
            # can still contain a UID that the authoritative set says has
            # already left the source folder.
            self._confirm_move_tombstones(target.id, result.all_uids)
        self._db.reassign_conversations(target.id)

        # From every fetched header, not just the newly added ones, so an
        # existing install fills its contacts on the next sync.
        self._db.save_contacts([a for m in result.messages for a in m.addresses])

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
            self._notify_new_mail(new_messages, target.id)
        return False

    def _notify_new_mail(
        self, messages: list[mail_sync.MessageHeader], folder_id: int
    ) -> None:
        if not self._settings.get_boolean("notifications"):
            return
        app = self.get_application()
        if app is None:
            return

        if len(messages) == 1:
            notification = Gio.Notification.new(messages[0].sender)
            notification.set_body(messages[0].subject)
            # Clicking it opens that thread, which is what marks it read.
            notification.set_default_action_and_target(
                "app.open-mail", GLib.Variant("(is)", (folder_id, messages[0].uid))
            )
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
        self._show_connection_banner(message, self._retry_button_label(category))
        return False

    @staticmethod
    def _retry_button_label(category: str) -> str:
        # Auth failures aren't worth a Retry button (same password); everything
        # else is a transient connection problem the user can retry.
        return "" if category == errors.CATEGORY_AUTH else _("Retry")

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
        if self._account is not None:
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
        if self._account is not None:
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
        self._settings.disconnect(self._avatar_handler)
        self._avatars.shutdown()
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

    # Rebuilding the tree destroys every row, which resets the user's
    # expand/collapse state, so only rebuild when the folders or their nesting
    # actually changed. A plain badge/icon update refreshes the rows in place.
    def _reload_folders(self) -> None:
        if self._account is None:
            return
        folders = self._db.folders_for_account(self._account.id)

        self._folder_children = {}
        for folder in folders:
            self._folder_children.setdefault(folder.parent_id, []).append(folder)

        shape = [(f.id, f.parent_id) for f in folders]
        if shape != self._folder_shape:
            self._folder_shape = shape
            self._rebuild_folder_tree()

        for folder in folders:
            row = self._folder_rows.get(folder.id)
            if row is not None:
                row.bind(folder, self._db.unread_count_in_folder(folder.id))

    def _rebuild_folder_tree(self) -> None:
        # Preserve the selection by folder id, not row index — pruning stale
        # folders shifts the indices. Re-selecting is suppressed so it doesn't
        # rebuild the conversation list; callers refresh that explicitly.
        keep_id = self._current_folder.id if self._current_folder else None

        self._suppress_folder_refresh = True

        self._folder_rows.clear()
        self._folder_root_store.remove_all()
        for folder in self._folder_children.get(None, []):
            self._folder_root_store.append(folder)

        if keep_id is not None:
            self._select_folder_by_id(keep_id)

        self._suppress_folder_refresh = False

    def _select_folder_by_id(self, folder_id: int) -> None:
        n = self._folder_tree_model.get_n_items()
        if n == 0:
            return
        for i in range(n):
            tree_row = self._folder_tree_model.get_item(i)
            if isinstance(tree_row, Gtk.TreeListRow):
                f = tree_row.get_item()
                if isinstance(f, Folder) and f.id == folder_id:
                    self._folder_selection.set_selected(i)
                    return

    # Open one message by IMAP UID (from a notification). Clearing _rendered_id
    # makes the reader rebuild even if the thread is already shown, so the usual
    # open path marks it read.
    def open_email(self, folder_id: int, uid: str) -> None:
        if self._account is None:
            return
        self._select_folder_by_id(folder_id)

        store = self._conversation_store
        for index in range(store.get_n_items()):
            conversation = store.get_item(index)
            if not isinstance(conversation, Conversation):
                continue
            if any(mail.server_id == uid for mail in conversation.emails):
                self._rendered_id = None
                # Gtk.MultiSelection has no set_selected -- that is
                # SingleSelection's API. Same idiom as _on_row_right_click.
                self._selection.unselect_all()
                self._selection.select_item(index, True)
                self._update_reader()
                return

    def _toast(self, text: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=text))


# ponytail: quotes are flattened to text; inlining the original's real HTML
# would need a sanitizer, since the composer runs with JavaScript enabled.
def _original_text(parsed: ParsedMessage | None) -> str:
    if parsed is None:
        return ""
    return parsed.text_body or compose.html_to_text(parsed.html_body or "")
