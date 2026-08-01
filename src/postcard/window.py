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

import logging

from gi.repository import Adw, Gio, GLib, Gtk

from .avatar_loader import AvatarLoader
from .core.models.account import Account
from .core.models.conversation import Conversation
from .core.models.folder import Folder
from .core.store.database import Database
from .folder_row import FolderRow
from .message_view import MessageView
from .preferences_dialog import SETTING_SYNC_INTERVAL
from .window_accounts import AccountsMixin
from .window_actions import MailActionsMixin
from .window_folders import FolderTreeMixin
from .window_list import ConversationListMixin
from .window_move import MoveMixin
from .window_reader import ReaderMixin
from .window_sync import SyncMixin
from .window_types import PAGE_EMPTY, PAGE_MAIL, PAGE_NO_ACCOUNT, PendingMove

logger = logging.getLogger(__name__)


@Gtk.Template(resource_path="/in/gxanshu/postcard/ui/main-window.ui")
class PostcardMainWindow(
    # Adw.ApplicationWindow comes last: the mixins share MainWindowParts, which
    # is the window type when type-checking, and C3 requires a base to follow
    # its own subclasses. Being last does not change the GType parent, so
    # Gtk.Template still sees AdwApplicationWindow.
    AccountsMixin,
    MailActionsMixin,
    MoveMixin,
    FolderTreeMixin,
    ConversationListMixin,
    ReaderMixin,
    SyncMixin,
    Adw.ApplicationWindow,
):
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
        self._pending_move: PendingMove | None = None
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
        self._is_online = self._network.get_network_available()
        self._network_handler = self._network.connect(
            "network-changed", self._on_network_changed
        )

        self._avatars = AvatarLoader(self._settings)
        self._avatar_handler = self._settings.connect(
            "changed::load-sender-avatars", lambda *_: self._refresh_conversations()
        )

        self._is_syncing = False
        self._sync_timer_id = 0
        self._interval_handler = self._settings.connect(
            f"changed::{SETTING_SYNC_INTERVAL}", lambda *_: self._reschedule_sync()
        )

        self.connect("close-request", self._on_close_request)
        if not self._is_online:
            self._show_offline_banner()

        accounts = self._db.accounts()
        if not accounts:
            self.main_stack.set_visible_child_name(PAGE_NO_ACCOUNT)
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
        self._loaded_counts: dict[int, int] = {}
        self._folders_with_more_mail: dict[int, bool] = {}
        self.reader_stack.set_visible_child_name(PAGE_EMPTY)
        self._set_mail_actions_enabled(False)
        self.reply_button.set_sensitive(False)
        self.forward_button.set_sensitive(False)
        self._set_reply_forward_enabled(False)

        self.main_stack.set_visible_child_name(PAGE_MAIL)
        self._refresh_account_switcher()

        # Boxes of the rows currently on screen, keyed by folder id, so
        # _reload_folders can refresh them without rebuilding the tree.
        self._folder_rows: dict[int, FolderRow] = {}
        # The (id, parent_id) pairs the tree was last built from.
        self._folder_shape_pairs: list[tuple[int, int | None]] = []
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
        if self._is_online:
            self._start_sync(in_background=True)

    # --- account switcher -------------------------------------------------

    # --- mail actions -----------------------------------------------------

    # --- archive / trash / move (with undo) -------------------------------

    # --- connection banner / offline handling -----------------------------

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
