from typing import TYPE_CHECKING

from gi.repository import Adw, Gio, Gtk

from .avatar_loader import AvatarLoader
from .core.models.account import Account
from .core.models.conversation import Conversation
from .core.models.email import Email
from .core.models.folder import Folder
from .core.store.database import Database
from .folder_row import FolderRow
from .mail_sync import FolderRole
from .message_view import MessageView
from .window_types import OutboxResult, PendingMove

if TYPE_CHECKING:
    # Type-checking only. The mixins also call the window's own inherited API
    # (get_application, lookup_action, add_action, is_active) and pass `self`
    # where a Gtk.Window is expected, so for pyright this base *is* the window.
    #
    # At runtime it must stay a plain object: Gtk.Template checks that the
    # template's declared parent matches the instance's direct parent GType, so
    # a mixin that were itself a GObject subclass would make PostcardMainWindow's
    # direct parent a mixin and the template would refuse to build.
    _Base = Adw.ApplicationWindow
else:
    _Base = object


class MainWindowParts(_Base):
    """Declarations only -- see the module docstring."""

    # --- widgets from main-window.blp -------------------------------------
    folder_list: Gtk.ListView
    conversation_list: Gtk.ListView
    conversation_scroller: Gtk.ScrolledWindow
    conversation_stack: Gtk.Stack
    reader_stack: Gtk.Stack
    reader_subject: Gtk.Label
    thread_box: Gtk.Box
    main_stack: Gtk.Stack
    account_switcher: Gtk.MenuButton
    add_account_button: Gtk.Button
    refresh_button: Gtk.Button
    sync_spinner: Gtk.Spinner
    search_bar: Gtk.SearchBar
    search_entry: Gtk.SearchEntry
    unread_button: Gtk.ToggleButton
    compose_button: Gtk.Button
    reply_button: Gtk.Button
    forward_button: Gtk.Button
    mark_read_button: Gtk.Button
    star_button: Gtk.Button
    move_button: Gtk.MenuButton
    toast_overlay: Adw.ToastOverlay
    connection_banner: Adw.Banner

    # --- collaborators ----------------------------------------------------
    _db: Database
    _settings: Gio.Settings
    _avatars: AvatarLoader

    # --- what is open -----------------------------------------------------
    _account: Account | None
    _current_folder: Folder | None
    _active_view: MessageView | None
    _rendered_id: int | None

    # --- folder sidebar ---------------------------------------------------
    _folder_rows: dict[int, FolderRow]
    _folder_shape_pairs: list[tuple[int, int | None]]
    _folder_children: dict[int | None, list[Folder]]
    _folder_root_store: Gio.ListStore
    _folder_tree_model: Gtk.TreeListModel
    _folder_selection: Gtk.SingleSelection
    _suppress_folder_refresh: bool
    _remote_unread_counts: dict[int, int]

    # --- conversation list ------------------------------------------------
    _conversation_store: Gio.ListStore
    _selection: Gtk.MultiSelection
    _selection_update_in_progress: bool
    _search_timeout: int
    # Load-on-scroll paging state, keyed by folder id.
    _loaded_counts: dict[int, int]
    _folders_with_more_mail: dict[int, bool]

    # --- pending move (see PendingMove) -----------------------------------
    _pending_move: PendingMove | None
    _pending_toast: Adw.Toast | None
    _move_tombstones: dict[tuple[int, str], dict[str, int]]

    # --- sync / connection state ------------------------------------------
    _is_syncing: bool
    _is_online: bool
    _has_notified_background: bool
    _sync_timer_id: int

    # --- cross-module methods ---------------------------------------------
    # Grouped by the module that implements them.

    # window.py
    def _load_mail_view(self, account: Account) -> None:
        raise NotImplementedError

    def _toast(self, text: str) -> None:
        raise NotImplementedError

    # window_accounts.py
    def _refresh_account_switcher(self) -> None:
        raise NotImplementedError

    def _on_add_account_clicked(self, _button: Gtk.Button) -> None:
        raise NotImplementedError

    def _on_compose_clicked(self, *_args: object) -> None:
        raise NotImplementedError

    def _on_reply_clicked(self, *_args: object) -> None:
        raise NotImplementedError

    def _on_forward_clicked(self, *_args: object) -> None:
        raise NotImplementedError

    # window_actions.py
    def _selected_conversations(self) -> list[Conversation]:
        raise NotImplementedError

    def _selected_conversation(self) -> Conversation | None:
        raise NotImplementedError

    def _set_mail_actions_enabled(self, is_enabled: bool) -> None:
        raise NotImplementedError

    def _set_reply_forward_enabled(self, is_enabled: bool) -> None:
        raise NotImplementedError

    def _mark_conversation_read(self, conversation: Conversation) -> None:
        raise NotImplementedError

    def _on_row_right_click(
        self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float, item: object
    ) -> None:
        raise NotImplementedError

    # window_move.py
    def _on_archive(self, _action: Gio.SimpleAction, _param: object) -> None:
        raise NotImplementedError

    def _on_trash(self, _action: Gio.SimpleAction, _param: object) -> None:
        raise NotImplementedError

    def _on_move(self, _action: Gio.SimpleAction, param: object) -> None:
        raise NotImplementedError

    def _build_move_menu(self, action_prefix: str = "win") -> Gio.Menu:
        raise NotImplementedError

    def _confirm_move_tombstones(self, folder_id: int, all_uids: set[str]) -> None:
        raise NotImplementedError

    def _start_move_by_role(
        self, role: FolderRole, conversation: Conversation | None = None
    ) -> None:
        raise NotImplementedError

    # window_folders.py
    def _setup_folder_sidebar(self) -> None:
        raise NotImplementedError

    def _folder_children_func(self, item: object) -> Gio.ListModel | None:
        raise NotImplementedError

    def _on_folder_selected(
        self, selection: Gtk.SingleSelection, position: int, n_items: int
    ) -> None:
        raise NotImplementedError

    def _reload_folders(self) -> None:
        raise NotImplementedError

    def _select_inbox_row(self) -> None:
        raise NotImplementedError

    def _select_folder_by_id(self, folder_id: int) -> None:
        raise NotImplementedError

    # window_list.py
    def _refresh_conversations(self, keep_id: int | None = None) -> None:
        raise NotImplementedError

    def _setup_conversation_list(self) -> None:
        raise NotImplementedError

    def _on_refresh_clicked(self, *_args: object) -> None:
        raise NotImplementedError

    def _on_search_changed(self, _entry: Gtk.SearchEntry) -> None:
        raise NotImplementedError

    def _on_search_mode_changed(self, _bar: Gtk.SearchBar, _param: object) -> None:
        raise NotImplementedError

    def _on_search_action(self, _action: Gio.SimpleAction, _param: object) -> None:
        raise NotImplementedError

    def _on_unread_toggled(self, _button: Gtk.ToggleButton) -> None:
        raise NotImplementedError

    # window_reader.py
    def _update_reader(self) -> None:
        raise NotImplementedError

    def _load_body(self, mail: Email, callback: object) -> None:
        raise NotImplementedError

    def _save_attachment(self, attachment: object) -> None:
        raise NotImplementedError

    # window_sync.py
    def _start_sync(
        self,
        in_background: bool = False,
        folder_name: str | None = None,
        offset: int = 0,
    ) -> None:
        raise NotImplementedError

    def _drain_outbox(self) -> None:
        raise NotImplementedError

    def _on_outbox_drained(self, results: list[OutboxResult]) -> bool:
        raise NotImplementedError

    def _reschedule_sync(self) -> None:
        raise NotImplementedError

    def _set_syncing(self, is_syncing: bool) -> None:
        raise NotImplementedError

    def _show_connection_banner(self, title: str, button_label: str = "") -> None:
        raise NotImplementedError

    def _show_offline_banner(self) -> None:
        raise NotImplementedError

    def _on_banner_retry(self, _banner: Adw.Banner) -> None:
        raise NotImplementedError

    def _on_network_changed(
        self, _monitor: Gio.NetworkMonitor, is_available: bool
    ) -> None:
        raise NotImplementedError

    def _notify_background(self) -> None:
        raise NotImplementedError
