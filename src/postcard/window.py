import email
import logging
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from email import policy
from email.utils import parseaddr
from gettext import gettext as _
from gettext import ngettext
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from . import mail_sync, message_view
from .account_dialog import PostcardAccountDialog
from .accounts_dialog import PostcardAccountsDialog
from .avatar_loader import AvatarLoader
from .composer_window import PostcardComposerWindow, composer_for_mailto
from .conversation_row import ConversationRow
from .core import compose, secrets
from .core.mime.message_parser import ParsedMessage, Unsubscribe
from .core.models.account import Account
from .core.models.attachment import Attachment
from .core.models.conversation import Conversation
from .core.models.email import Email
from .core.models.folder import Folder
from .core.net import errors, imap_session
from .core.store.database import Database
from .folder_row import FolderRow
from .message_view import LoadCallback, MessageView
from .online_accounts_dialog import PostcardOnlineAccountsDialog
from .window_types import (
    FOLDER_SYNC_COOLDOWN_SECONDS,
    MAIL_ACTIONS,
    MOVE_UNDO_MS,
    PAGE_EMPTY,
    PAGE_LIST,
    PAGE_LOADING,
    PAGE_MAIL,
    PAGE_NO_ACCOUNT,
    REPLY_FORWARD_ACTIONS,
    SEARCH_DEBOUNCE_MS,
    SECONDS_PER_MINUTE,
    SETTING_SYNC_INTERVAL,
    BodyRequest,
    FlagChange,
    OutboxResult,
    PendingMove,
)

if TYPE_CHECKING:
    from .application import PostcardApplication

logger = logging.getLogger(__name__)

SETTING_FOLDER_WIDTH = "folder-sidebar-width"
SETTING_CONVERSATION_WIDTH = "conversation-sidebar-width"

# Move is the one action carrying a parameter (the destination folder name), so
# it is registered on its own wherever these are.
_MOVE_PARAM_TYPE = "s"


def _register(
    target: Gio.ActionMap,
    name: str,
    handler: Callable[..., None],
    param_type: str | None = None,
) -> None:
    """Add one activatable action to a window or a context menu's group."""
    action = Gio.SimpleAction.new(
        name, GLib.VariantType.new(param_type) if param_type else None
    )
    action.connect("activate", handler)
    target.add_action(action)


# ponytail: quotes are flattened to text; inlining the original's real HTML
# would need a sanitizer, since the composer runs with JavaScript enabled.
def _original_text(parsed: ParsedMessage | None) -> str:
    if parsed is None:
        return ""
    return parsed.text_body or compose.html_to_text(parsed.html_body or "")


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
    add_account_button: Gtk.Button = Gtk.Template.Child()
    online_accounts_button: Gtk.Button = Gtk.Template.Child()
    refresh_button: Gtk.Button = Gtk.Template.Child()
    search_bar: Gtk.SearchBar = Gtk.Template.Child()
    search_entry: Gtk.SearchEntry = Gtk.Template.Child()
    unread_button: Gtk.ToggleButton = Gtk.Template.Child()
    compose_button: Gtk.Button = Gtk.Template.Child()
    reply_all_button: Gtk.Button = Gtk.Template.Child()
    reply_button: Gtk.Button = Gtk.Template.Child()
    forward_button: Gtk.Button = Gtk.Template.Child()
    mark_read_button: Gtk.Button = Gtk.Template.Child()
    star_button: Gtk.Button = Gtk.Template.Child()
    archive_button: Gtk.Button = Gtk.Template.Child()
    archive_button_content: Adw.ButtonContent = Gtk.Template.Child()
    move_button: Gtk.MenuButton = Gtk.Template.Child()
    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()
    connection_banner: Adw.Banner = Gtk.Template.Child()
    outer_split: Adw.NavigationSplitView = Gtk.Template.Child()
    inner_split: Adw.NavigationSplitView = Gtk.Template.Child()
    folder_resize_handle: Gtk.Box = Gtk.Template.Child()
    conversation_resize_handle: Gtk.Box = Gtk.Template.Child()

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

        # Drag limits mirror the <range> in the gschema: the schema stops a bad
        # stored value, these stop a drag from producing one.
        # ponytail: pointer-only resize, add a key-pressed controller on the
        # handles if keyboard resizing turns out to matter.
        self._setup_sidebar_resize(
            self.folder_resize_handle, self.outer_split, SETTING_FOLDER_WIDTH, 180, 500
        )
        self._setup_sidebar_resize(
            self.conversation_resize_handle,
            self.inner_split,
            SETTING_CONVERSATION_WIDTH,
            220,
            600,
        )

        # The account owning the selected folder. None until _load_mail_view
        # runs, which __init__ skips when there are no accounts yet, so read it
        # through a guard clause -- background callbacks can fire before then.
        self._account: Account | None = None
        # Every account, by id.
        self._accounts: dict[int, Account] = {}

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

        self._connect_widgets()

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

        # Accounts with a sync in flight. A set, not a flag: every account syncs
        # on the same tick.
        self._syncing_account_ids: set[int] = set()
        self._sync_timer_id = 0
        self._interval_handler = self._settings.connect(
            f"changed::{SETTING_SYNC_INTERVAL}", lambda *_: self._reschedule_sync()
        )

        self.connect("close-request", self._on_close_request)
        if not self._is_online:
            self._show_offline_banner()

        self._build_mail_models()

        # Gio.SimpleAction starts enabled, so the accelerators would stay live
        # on a window with nothing selected to act on.
        self._set_mail_actions_enabled(False)
        self._set_reply_forward_enabled(False)

        if not self._db.accounts():
            self.main_stack.set_visible_child_name(PAGE_NO_ACCOUNT)
            return

        self._load_mail_view()

    def _connect_widgets(self) -> None:
        self.add_account_button.connect("clicked", self._on_add_account_clicked)
        self.online_accounts_button.connect("clicked", self._on_online_accounts_clicked)
        self.refresh_button.connect("clicked", self._on_refresh_clicked)
        self.compose_button.connect("clicked", self._on_compose_clicked)
        self.reply_all_button.connect("clicked", self._on_reply_all_clicked)
        self.reply_button.connect("clicked", self._on_reply_clicked)
        self.forward_button.connect("clicked", self._on_forward_clicked)

        self.search_bar.set_key_capture_widget(self)
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_bar.connect(
            "notify::search-mode-enabled", self._on_search_mode_changed
        )
        self.unread_button.connect("toggled", self._on_unread_toggled)

        # Load older mail when the list is scrolled to the bottom.
        self.conversation_scroller.connect("edge-reached", self._on_list_edge_reached)
        # Presenting the window again after _on_close_request released the
        # reading pane re-renders whatever is still selected.
        self.connect("map", self._on_map)

    # Everything the mail view runs on. Built once, before we know
    # whether there are any accounts to put in it.
    def _build_mail_models(self) -> None:
        # Load-on-scroll paging state, keyed by folder id: how many of the
        # newest messages we've paged through (also the next page's offset),
        # and whether older messages remain on the server.
        self._loaded_counts: dict[int, int] = {}
        self._folders_with_more_mail: dict[int, bool] = {}
        # When each folder was last synced, so revisiting one doesn't refetch.
        self._folder_sync_times: dict[int, float] = {}
        # Unread counts the server last reported for the folders we don't
        # fetch, keyed by folder id.
        self._remote_unread_counts: dict[int, int] = {}

        # Boxes of the rows currently on screen, keyed by folder id, so
        # _reload_folders can refresh them without rebuilding the tree.
        self._folder_rows: dict[int, FolderRow] = {}
        # The same, for the account headings, so their sync spinners can start
        # and stop on their own.
        self._account_rows: dict[int, FolderRow] = {}
        # The account ids and (id, parent_id) folder pairs the tree was last
        # built from.
        self._folder_shape: tuple[list[int], list[tuple[int, int | None]]] = ([], [])
        # Top-level folders per account id, and child folders per parent id.
        self._account_roots: dict[int, list[Folder]] = {}
        self._folder_children: dict[int, list[Folder]] = {}

        self._folder_root_store: Gio.ListStore = Gio.ListStore(item_type=Account)
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

    # Show the mail view over every account at once. Runs once per window, or
    # again when the first account is added to an empty database.
    def _load_mail_view(self) -> None:
        self.reader_stack.set_visible_child_name(PAGE_EMPTY)
        self.main_stack.set_visible_child_name(PAGE_MAIL)

        # _reload_folders selects the inbox, which fetches it via
        # _on_folder_selected; the sync below covers the other accounts, and
        # bootstraps a fresh one whose folders aren't in the database yet.
        self._reload_folders()

        self._reschedule_sync()
        if self._is_online:
            self._sync_all(in_background=True)

    def _setup_sidebar_resize(
        self,
        handle: Gtk.Box,
        split: Adw.NavigationSplitView,
        key: str,
        lower: int,
        upper: int,
    ) -> None:
        """Let `handle` drag `split`'s sidebar, remembering the width under `key`.

        libadwaita has no resizable split view, so the width is pinned by
        setting the sidebar's minimum and maximum to the same value.
        """
        self._pin_sidebar_width(
            split, min(max(self._settings.get_int(key), lower), upper)
        )
        handle.set_cursor(Gdk.Cursor.new_from_name("col-resize", None))
        # A collapsed sidebar fills the window, which would leave the handle
        # stranded over the middle of the content.
        split.bind_property(
            "collapsed",
            handle,
            "visible",
            GObject.BindingFlags.SYNC_CREATE | GObject.BindingFlags.INVERT_BOOLEAN,
        )

        start_width = 0.0
        start_x = 0.0
        gesture = Gtk.GestureDrag()

        def on_begin(gesture: Gtk.GestureDrag, _x: float, _y: float) -> None:
            nonlocal start_width, start_x
            start_width = split.get_min_sidebar_width()
            start_x = self._pointer_x(gesture)

        def on_update(gesture: Gtk.GestureDrag, _x: float, _y: float) -> None:
            width = start_width + self._pointer_x(gesture) - start_x
            self._pin_sidebar_width(split, min(max(int(width), lower), upper))

        gesture.connect("drag-begin", on_begin)
        gesture.connect("drag-update", on_update)
        handle.add_controller(gesture)

    @staticmethod
    def _pointer_x(gesture: Gtk.GestureDrag) -> float:
        """The pointer's x within the window, not within the handle.

        GestureDrag reports offsets in the handle's own coordinates, and the
        handle rides the trailing edge of the sidebar it resizes: widening
        moves it right, which shrinks the reported offset, which narrows the
        sidebar again. Surface coordinates don't move underneath the drag.
        """
        event = gesture.get_last_event(gesture.get_current_sequence())
        if event is None:
            return 0.0
        _found, x, _y = event.get_position()
        return x

    @staticmethod
    def _pin_sidebar_width(split: Adw.NavigationSplitView, width: int) -> None:
        split.set_max_sidebar_width(width)
        split.set_min_sidebar_width(width)

    def _on_close_request(self, _window: Gtk.Window) -> bool:
        width, height = self.get_default_size()
        self._settings.set_int("window-width", width)
        self._settings.set_int("window-height", height)
        self._settings.set_boolean("window-maximized", self.is_maximized())
        self._settings.set_int(
            SETTING_FOLDER_WIDTH, int(self.outer_split.get_min_sidebar_width())
        )
        self._settings.set_int(
            SETTING_CONVERSATION_WIDTH, int(self.inner_split.get_min_sidebar_width())
        )
        # Nothing on screen to render, so give the ~300 MB web process back.
        message_view.release_anchor()

        if self._settings.get_boolean("run-in-background"):
            # _on_map renders the reading pane again when the window returns.
            self._rendered_id = None
            self._active_view = None
            self._clear_thread()
            self.reader_stack.set_visible_child_name(PAGE_EMPTY)

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

    def _on_map(self, _window: Gtk.Window) -> None:
        # _update_reader guards on _account, so the first map of a window built
        # on an empty database does nothing.
        self._update_reader()

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

    # --- accounts, and opening the composer -------------------------------

    def _on_manage_accounts(self, *_args: object) -> None:
        dialog = PostcardAccountsDialog(self._db)
        dialog.connect("closed", lambda *_: self.reload_accounts())
        dialog.present(self)

    # Re-read accounts after they change (add/remove). _reload_folders picks a
    # new folder if the open one went with a deleted account.
    def reload_accounts(self) -> None:
        if not self._db.accounts():
            self._accounts = {}
            self._account = None
            self._current_folder = None
            self.main_stack.set_visible_child_name(PAGE_NO_ACCOUNT)
            self._push_tray_unread([])
            return
        if self._account is None:
            self._load_mail_view()
            return
        self._reload_folders()

    def _on_add_account_clicked(self, *_args: object) -> None:
        dialog = PostcardAccountDialog(self._db)
        dialog.connect("account-added", self._on_account_added)
        dialog.present(self)

    def _on_online_accounts_clicked(self, *_args: object) -> None:
        dialog = PostcardOnlineAccountsDialog(self._db)
        dialog.connect("account-added", self._on_account_added)
        dialog.present(self)

    def _on_account_added(self, _dialog: Adw.Dialog) -> None:
        # The first account has no mail view yet; a later one only adds a branch
        # to the sidebar, so the open folder is left alone.
        if self._current_folder is None:
            self._load_mail_view()
            return
        self._reload_folders()
        # Highest id sorts last, so this is the one just added.
        self._start_sync(self._db.accounts()[-1], in_background=True)

    def _signature_text(self) -> str:
        if not self._settings.get_boolean("signature-enabled"):
            return ""
        return self._settings.get_string("signature-text").strip()

    def _on_compose_clicked(self, *_args: object) -> None:
        # The tray can reach this before any account exists.
        if self._account is None:
            return
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
        if self._account is None:
            return
        composer = composer_for_mailto(
            self.get_application(), self._db, self._account, self._settings, uri
        )
        composer.connect("finished", self._on_composer_finished)
        composer.present()

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
        selected = self._selected_conversation()
        keep_id = selected.id if selected is not None else None
        self._reload_folders()
        self._refresh_conversations(keep_id=keep_id)
        if self._account is not None:
            self._drain_outbox(self._account)

    # --- read/unread, star, and the row context menu ----------------------

    def _setup_actions(self) -> None:
        for name, handler in (
            ("toggle-read", self._on_toggle_read),
            ("toggle-star", self._on_toggle_star),
            ("archive", self._on_archive),
            ("trash", self._on_trash),
            ("compose", self._on_compose_clicked),
            ("reply", self._on_reply_clicked),
            ("reply-all", self._on_reply_all_clicked),
            ("forward", self._on_forward_clicked),
            ("refresh", self._on_refresh_clicked),
            ("search", self._on_search_action),
            ("add-account", self._on_add_account_clicked),
            ("online-accounts", self._on_online_accounts_clicked),
            ("manage-accounts", self._on_manage_accounts),
        ):
            _register(self, name, handler)
        _register(self, "move", self._on_move, _MOVE_PARAM_TYPE)

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
                ("win.reply-all", ["<ctrl><shift>r"]),
                ("win.forward", ["<ctrl><shift>f"]),
                ("win.refresh", ["F5"]),
                ("win.search", ["<ctrl>f"]),
            ):
                app.set_accels_for_action(name, accels)

    def _set_actions_enabled(self, names: tuple[str, ...], is_enabled: bool) -> None:
        # Only Gio.SimpleAction can be toggled; the plain Gio.Action interface
        # exposes no setter, so anything else is skipped rather than crashing.
        for name in names:
            action = self.lookup_action(name)
            if isinstance(action, Gio.SimpleAction):
                action.set_enabled(is_enabled)

    def _set_mail_actions_enabled(self, is_enabled: bool) -> None:
        self._set_actions_enabled(MAIL_ACTIONS, is_enabled)
        self.move_button.set_sensitive(is_enabled)

    def _set_reply_forward_enabled(self, is_enabled: bool) -> None:
        self._set_actions_enabled(REPLY_FORWARD_ACTIONS, is_enabled)
        for button in (self.reply_button, self.reply_all_button, self.forward_button):
            button.set_sensitive(is_enabled)

    def _selected_conversations(self) -> list[Conversation]:
        # The store is built by _load_mail_view, so before an account is open
        # there is nothing selected rather than an error. Every mail action
        # funnels through here, which is why the guard belongs here.
        if self._account is None:
            return []
        # Ask the selection which positions are set rather than asking every
        # position whether it is selected: this runs several times per
        # selection change and per sync, over the whole folder.
        positions = self._selection.get_selection()
        selected = []
        for index in range(positions.get_size()):
            conversation = self._conversation_store.get_item(positions.get_nth(index))
            if isinstance(conversation, Conversation):
                selected.append(conversation)
        return selected

    def _selected_conversation(self) -> Conversation | None:
        selected = self._selected_conversations()
        return selected[0] if len(selected) == 1 else None

    def _on_toggle_read(self, _action: Gio.SimpleAction, _param: object) -> None:
        conversations = self._selected_conversations()
        if conversations:
            self._toggle_read(conversations)

    def _on_toggle_star(self, _action: Gio.SimpleAction, _param: object) -> None:
        conversations = self._selected_conversations()
        if conversations:
            self._toggle_flag(
                conversations,
                "is_starred",
                self._db.set_email_starred,
                imap_session.FLAG_FLAGGED,
            )

    def _toggle_read(self, conversations: list[Conversation]) -> None:
        self._toggle_flag(
            conversations,
            "is_unread",
            self._db.set_email_unread,
            imap_session.FLAG_SEEN,
            is_flag_inverted=True,
        )

    # Clear the is_unread flag for a whole conversation: locally, in the badges
    # and list, and on the server. Guarded rather than routed straight through
    # _toggle_flag, which on an already-read thread would flip it back to unread.
    def _mark_conversation_read(self, conversation: Conversation) -> None:
        if conversation.is_unread:
            self._toggle_read([conversation])

    def _toggle_flag(
        self,
        conversations: list[Conversation],
        field: str,
        save: Callable[[int, bool], None],
        flag: str,
        is_flag_inverted: bool = False,
    ) -> None:
        """Flip one boolean flag across a selection, locally and on the server.

        A mixed selection follows the aggregate command shown in the menu: if
        anything is unread, the whole selection is marked read. `field` names
        the Email attribute and `save` the Database setter that records it;
        is_flag_inverted covers \\Seen, which is the opposite of is_unread.
        """
        mails = [mail for conversation in conversations for mail in conversation.emails]
        value = not any(getattr(mail, field) for mail in mails)
        originals = {mail.id: getattr(mail, field) for mail in mails}

        def write(values: dict[int, bool]) -> None:
            for mail in mails:
                setattr(mail, field, values[mail.id])
                save(mail.id, values[mail.id])

        def revert() -> None:
            write(originals)
            self._after_flag_change(conversations)

        write(dict.fromkeys(originals, value))
        self._after_flag_change(conversations)
        uids = [
            uid
            for conversation in conversations
            for uid in mail_sync.server_uids(conversation)
        ]
        self._run_flag_worker(
            uids,
            flag,
            should_add=not value if is_flag_inverted else value,
            revert=revert,
        )

    # Update badges and the list after a flag change, keeping this
    # conversation selected (so the reader doesn't reload).
    def _after_flag_change(self, conversations: list[Conversation]) -> None:
        keep_id = conversations[0].id if len(conversations) == 1 else None
        self._reload_folders()
        self._refresh_conversations(keep_id=keep_id)

    def _run_flag_worker(
        self, uids: list[str], flag: str, should_add: bool, revert: Callable[[], None]
    ) -> None:
        account = self._account
        folder = self._current_folder
        if account is None or folder is None:
            return
        change = FlagChange(
            folder_name=folder.name,
            uids=tuple(uids),
            flag=flag,
            should_add=should_add,
        )
        thread = threading.Thread(
            target=self._flag_worker,
            args=(account, change, revert),
            daemon=True,
        )
        thread.start()

    # Runs on the worker thread: network only, no Gtk/database access.
    def _flag_worker(
        self,
        account: Account,
        change: FlagChange,
        revert: Callable[[], None],
    ) -> None:
        credential = secrets.credential_for(account)
        if credential is None:
            logger.warning("could not sign in to account %s", account.email)
            GLib.idle_add(self._on_flag_sign_in_failed, revert)
            return

        try:
            mail_sync.set_flag(
                account,
                credential,
                change.folder_name,
                change.uids,
                change.flag,
                change.should_add,
            )
        except Exception as error:
            logger.exception(
                "could not set %s on %d message(s) in %s (account %s)",
                change.flag,
                len(change.uids),
                change.folder_name,
                account.email,
            )
            GLib.idle_add(self._on_action_failed, revert, account.imap_host, error)

    def _on_flag_sign_in_failed(self, revert: Callable[[], None]) -> bool:
        # The row was already updated optimistically, so leaving it would show a
        # read/starred state the server never got, until the next sync undid it.
        revert()
        self._toast(_("Could not sign in to this account."))
        return False

    def _on_action_failed(
        self, revert: Callable[[], None], host: str, error: Exception
    ) -> bool:
        revert()
        _is_auth_failure, message = errors.classify(error, host)
        self._toast(_("Action failed: {msg}").format(msg=message))
        return False

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
        popover.insert_action_group("context", self._context_actions())
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

    def _context_actions(self) -> Gio.SimpleActionGroup:
        """The subset of the window's actions the row context menu offers."""
        actions = Gio.SimpleActionGroup()
        for name, handler in (
            ("toggle-read", self._on_toggle_read),
            ("toggle-star", self._on_toggle_star),
            ("archive", self._on_archive),
            ("trash", self._on_trash),
        ):
            _register(actions, name, handler)
        _register(actions, "move", self._on_move, _MOVE_PARAM_TYPE)
        return actions

    def _context_menu(self, conversation: Conversation) -> Gio.Menu:
        menu = Gio.Menu()

        selected = self._selected_conversations() or [conversation]

        flags = Gio.Menu()
        read = (
            _("Mark Read")
            if any(item.is_unread for item in selected)
            else _("Mark Unread")
        )
        star = _("Unstar") if any(item.is_starred for item in selected) else _("Star")
        flags.append(read, "context.toggle-read")
        flags.append(star, "context.toggle-star")
        menu.append_section(None, flags)

        actions = Gio.Menu()
        actions.append(self._archive_label(), "context.archive")
        actions.append(_("Delete"), "context.trash")
        actions.append_submenu(_("Move to"), self._build_move_menu("context"))
        menu.append_section(None, actions)

        return menu

    # --- archive, trash and move, with an undo window ---------------------

    # Archive moves to the archive folder -- except while reading the archive
    # itself, where the same button/action/menu item unarchives to the inbox.
    def _archive_role(self) -> mail_sync.FolderRole:
        folder = self._current_folder
        if folder is not None and (
            mail_sync.role_for_folder(folder.name) is mail_sync.FolderRole.ARCHIVE
        ):
            return mail_sync.FolderRole.INBOX
        return mail_sync.FolderRole.ARCHIVE

    def _archive_label(self) -> str:
        if self._archive_role() is mail_sync.FolderRole.INBOX:
            return _("Unarchive")
        return _("Archive")

    def _update_archive_button(self) -> None:
        is_unarchive = self._archive_role() is mail_sync.FolderRole.INBOX
        label = self._archive_label()
        self.archive_button_content.set_label(label)
        self.archive_button_content.set_icon_name(
            "mail-unread-symbolic" if is_unarchive else "mail-archive-symbolic"
        )
        self.archive_button.set_tooltip_text(label)

    def _on_archive(self, _action: Gio.SimpleAction, _param: object) -> None:
        self._start_move_by_role(self._archive_role())

    def _on_trash(self, _action: Gio.SimpleAction, _param: object) -> None:
        self._start_move_by_role(mail_sync.FolderRole.TRASH)

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
        self, role: mail_sync.FolderRole, conversation: Conversation | None = None
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
        if role == mail_sync.FolderRole.ARCHIVE:
            title = ngettext("Archived", "Archived {n} conversations", count).format(
                n=count
            )
        elif role == mail_sync.FolderRole.INBOX:
            title = ngettext(
                "Unarchived", "Unarchived {n} conversations", count
            ).format(n=count)
        else:
            title = ngettext("Deleted", "Deleted {n} conversations", count).format(
                n=count
            )
        self._start_move(conversations, dest, title)

    def _folder_with_role(self, role: mail_sync.FolderRole) -> Folder | None:
        if self._account is None:
            return None
        current_id = self._current_folder.id if self._current_folder else None
        matches = [
            folder
            for folder in self._db.folders_for_account(self._account.id)
            if folder.id != current_id
            and mail_sync.role_for_folder(folder.name) == role
        ]
        if role == mail_sync.FolderRole.ARCHIVE:
            # role_for_folder maps both "Archive" and Gmail's "All Mail" to
            # ARCHIVE. When an account has both, a folder actually named
            # Archive is the one the user means -- All Mail is a view of
            # everything, so moving there wouldn't remove it from the inbox.
            for folder in matches:
                if mail_sync.FolderRole.ARCHIVE in folder.name.lower():
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
        account = self._account
        source = self._current_folder
        if account is None or source is None or dest.id == source.id:
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
        self._pending_move = PendingMove(
            account=account,
            email_ids=email_ids,
            uids=uids,
            originals=originals,
            source=source,
            dest=dest,
            tombstones=tombstones,
            timeout_id=GLib.timeout_add(MOVE_UNDO_MS, self._on_move_timeout),
        )
        self.toast_overlay.add_toast(toast)

    def _on_undo_move(self, _toast: Adw.Toast) -> None:
        pending = self._pending_move
        if pending is None:
            return
        GLib.source_remove(pending.timeout_id)
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
        GLib.source_remove(pending.timeout_id)
        self._pending_move = None
        if self._pending_toast is not None:
            self._pending_toast.dismiss()
            self._pending_toast = None
        self._run_move_worker(pending)

    def _restore_move(self, pending: PendingMove) -> None:
        self._db.reconcile_moved_emails(pending.originals)
        self._clear_move_tombstones(pending)
        self._reload_folders()
        self._refresh_conversations()

    def _clear_move_tombstones(self, pending: PendingMove, start: int = 0) -> None:
        for tombstone in pending.tombstones[start:]:
            state = self._move_tombstones.get(tombstone)
            if state is None:
                continue
            state["active"] -= 1
            if state["active"] <= 0 and state["awaiting"] <= 0:
                self._move_tombstones.pop(tombstone, None)

    def _await_move_tombstones(self, pending: PendingMove, completed: int) -> None:
        for tombstone in pending.tombstones[:completed]:
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

    def _run_move_worker(self, pending: PendingMove) -> None:
        thread = threading.Thread(
            target=self._move_worker,
            args=(pending.account, pending),
            daemon=True,
        )
        thread.start()

    # Runs on the worker thread: network only, no Gtk/database access.
    def _move_worker(self, account: Account, pending: PendingMove) -> None:
        credential = secrets.credential_for(account)
        if credential is None:
            logger.warning("could not sign in to account %s", account.email)
            GLib.idle_add(self._on_move_sign_in_failed, pending)
            return

        try:
            result = mail_sync.move_messages(
                account,
                credential,
                pending.source.name,
                pending.uids,
                pending.dest.name,
            )
            GLib.idle_add(self._on_move_result, pending, result)
        except Exception as error:
            logger.exception(
                "could not move %d message(s) from %s to %s (account %s)",
                len(pending.uids),
                pending.source.name,
                pending.dest.name,
                account.email,
            )
            GLib.idle_add(self._on_move_failed, pending, account.imap_host, error)

    def _on_move_result(
        self, pending: PendingMove, result: mail_sync.MoveResult
    ) -> bool:
        completed = len(result.destination_uids)
        self._await_move_tombstones(pending, completed)
        completed_moves = list(
            zip(
                pending.email_ids[:completed],
                [pending.dest.id] * completed,
                result.destination_uids,
                strict=True,
            )
        )
        if completed_moves:
            self._db.reconcile_moved_emails(completed_moves)
        failed_index = result.failed_index
        if failed_index is None and completed < len(pending.uids):
            failed_index = completed
        if failed_index is not None:
            self._db.reconcile_moved_emails(pending.originals[failed_index:])
            self._clear_move_tombstones(pending, failed_index)
        self._reload_folders()
        self._refresh_conversations()
        if result.error is not None:
            logger.warning(
                "move stopped after %d of %d message(s) from %s to %s: %s",
                completed,
                len(pending.uids),
                pending.source.name,
                pending.dest.name,
                result.error,
            )
            self._toast(_("Move failed: {msg}").format(msg=result.error))
        return False

    def _on_move_sign_in_failed(self, pending: PendingMove) -> bool:
        # The messages were already moved locally, so they have to come back.
        self._restore_move(pending)
        self._toast(_("Could not sign in to this account."))
        return False

    def _on_move_failed(
        self, pending: PendingMove, host: str, error: Exception
    ) -> bool:
        self._restore_move(pending)
        _is_auth_failure, message = errors.classify(error, host)
        self._toast(_("Move failed: {msg}").format(msg=message))
        return False

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

    # --- the folder sidebar -----------------------------------------------

    # Two kinds of branch: an account row holds its top-level mailboxes, a
    # folder row holds its subfolders.
    def _folder_children_func(
        self, item: GObject.Object, *_args: object
    ) -> Gio.ListStore | None:
        if isinstance(item, Account):
            children = self._account_roots.get(item.id)
        else:
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

        row = expander.get_child()
        assert isinstance(row, FolderRow)

        entry = tree_list_row.get_item()
        # An account row is a heading over its folders, not somewhere to click.
        item.set_selectable(isinstance(entry, Folder))
        if isinstance(entry, Account):
            self._account_rows[entry.id] = row
            is_syncing = entry.id in self._syncing_account_ids
            row.bind_account(entry, tree_list_row, is_syncing)
            return

        assert isinstance(entry, Folder)
        self._folder_rows[entry.id] = row
        row.bind(entry, self._unread_badge(entry))

    def _unread_badge(self, folder: Folder) -> int:
        """What the sidebar shows next to a folder.

        Only the open folder's messages are synced, so its local count is the
        accurate one -- and it drops the moment a message is read. Every other
        folder shows what the server last reported, since local rows there are
        whatever an earlier visit happened to leave behind.
        """
        if self._current_folder is not None and folder.id == self._current_folder.id:
            return self._db.unread_count_in_folder(folder.id)
        remote = self._remote_unread_counts.get(folder.id)
        if remote is None:
            return self._db.unread_count_in_folder(folder.id)
        return remote

    def _on_folder_row_unbind(
        self, _factory: Gtk.SignalListItemFactory, item: Gtk.ListItem
    ) -> None:
        tree_list_row = item.get_item()
        if isinstance(tree_list_row, Gtk.TreeListRow):
            entry = tree_list_row.get_item()
            if isinstance(entry, Folder):
                self._folder_rows.pop(entry.id, None)
            elif isinstance(entry, Account):
                self._account_rows.pop(entry.id, None)

        expander = item.get_child()
        if isinstance(expander, Gtk.TreeExpander):
            expander.set_list_row(None)

    # Select the first account's inbox, or its first folder if we can't spot one.
    def _select_inbox_row(self) -> None:
        target = -1
        for position, folder in self._folder_positions():
            if target < 0:
                target = position
            if mail_sync.role_for_folder(folder.name) == mail_sync.FolderRole.INBOX:
                target = position
                break
        if target < 0:
            return

        # Row 0 is autoselected when the tree is built, so set_selected() may
        # emit nothing. Load the folder directly instead.
        self._suppress_folder_refresh = True
        self._folder_selection.set_selected(target)
        self._suppress_folder_refresh = False
        self._current_folder = None
        self._on_folder_selected(self._folder_selection, target, 1)

    # Every folder row in the flattened tree, with its position. Account rows
    # sit in the same model, so they are skipped here rather than at each caller.
    def _folder_positions(self) -> Iterator[tuple[int, Folder]]:
        for position in range(self._folder_tree_model.get_n_items()):
            tree_row = self._folder_tree_model.get_item(position)
            if isinstance(tree_row, Gtk.TreeListRow):
                folder = tree_row.get_item()
                if isinstance(folder, Folder):
                    yield position, folder

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
        # SingleSelection autoselects row 0, which is an account heading.
        if not isinstance(folder, Folder):
            return
        previous = self._current_folder
        self._current_folder = folder
        self._account = self._accounts[folder.account_id]
        self.move_button.set_menu_model(self._build_move_menu())
        self._update_archive_button()
        if self._suppress_folder_refresh:
            return
        self._refresh_conversations()
        # Only sync on a real folder change — rebuilding the sidebar re-emits
        # selection-changed for the same folder, which would loop. A folder
        # synced moments ago is left alone: clicking back and forth between two
        # of them otherwise refetches both every time.
        changed = previous is None or previous.id != folder.id
        age = time.monotonic() - self._folder_sync_times.get(folder.id, 0.0)
        if changed and self._is_online and age >= FOLDER_SYNC_COOLDOWN_SECONDS:
            self._start_sync(self._account, in_background=True, folder_name=folder.name)

    # Rebuilding the tree destroys every row, which resets the user's
    # expand/collapse state, so only rebuild when the accounts, the folders or
    # their nesting actually changed. A plain badge/icon update refreshes the
    # rows in place.
    def _reload_folders(self) -> None:
        accounts = self._db.accounts()
        self._accounts = {account.id: account for account in accounts}
        folders = [
            folder
            for account in accounts
            for folder in self._db.folders_for_account(account.id)
        ]
        # Two kinds of branch: top-level folders hang off their account, the
        # rest off their parent folder.
        self._account_roots = {}
        self._folder_children = {}
        for folder in folders:
            if folder.parent_id is None:
                self._account_roots.setdefault(folder.account_id, []).append(folder)
            else:
                self._folder_children.setdefault(folder.parent_id, []).append(folder)

        shape = (
            [account.id for account in accounts],
            [(folder.id, folder.parent_id) for folder in folders],
        )
        if shape != self._folder_shape:
            self._folder_shape = shape
            self._rebuild_folder_tree(accounts)

        # SQLite reuses the rowid of a deleted folder, so anything keyed by
        # folder id has to go when the folder does -- otherwise a new account
        # inherits the old one's badge, cooldown and paging state.
        live_ids = {folder.id for folder in folders}
        for cache in (
            self._loaded_counts,
            self._folders_with_more_mail,
            self._folder_sync_times,
            self._remote_unread_counts,
        ):
            for folder_id in set(cache) - live_ids:
                del cache[folder_id]

        # Nothing is selected on a first run, or after the open folder was
        # pruned along with its account. An account row can't stand in: it is
        # only a heading, so the list would sit empty with no way back.
        if self._current_folder is not None and self._current_folder.id not in live_ids:
            self._current_folder = None
        if self._current_folder is None:
            self._select_inbox_row()

        for folder in folders:
            row = self._folder_rows.get(folder.id)
            if row is not None:
                row.bind(folder, self._unread_badge(folder))

        self._push_tray_unread(folders)

    def _push_tray_unread(self, folders: list[Folder]) -> None:
        app = cast("PostcardApplication | None", self.get_application())
        if app is None:
            return
        app.tray.set_unread(
            sum(
                self._unread_badge(folder)
                for folder in folders
                if mail_sync.role_for_folder(folder.name) is mail_sync.FolderRole.INBOX
            )
        )

    def _rebuild_folder_tree(self, accounts: list[Account]) -> None:
        # Preserve the selection by folder id, not row index — pruning stale
        # folders shifts the indices. Re-selecting is suppressed so it doesn't
        # rebuild the conversation list; callers refresh that explicitly.
        keep_id = self._current_folder.id if self._current_folder else None

        self._suppress_folder_refresh = True

        self._folder_rows.clear()
        self._account_rows.clear()
        self._folder_root_store.remove_all()
        for account in accounts:
            self._folder_root_store.append(account)

        if keep_id is not None:
            self._select_folder_by_id(keep_id)

        self._suppress_folder_refresh = False

    def _select_folder_by_id(self, folder_id: int) -> None:
        for position, folder in self._folder_positions():
            if folder.id == folder_id:
                self._folder_selection.set_selected(position)
                return

    # --- the conversation list: contents, search, and paging --------------

    # Rebuild the conversation list from the current folder, applying the
    # search query if one is typed. Called on folder change and search change.
    # keep_id re-selects that conversation if it's still in the list, so a mail
    # action can refresh without reloading the reader.
    def _refresh_conversations(self, keep_id: int | None = None) -> None:
        folder = self._current_folder
        if folder is None:
            return

        vadjustment = self.conversation_scroller.get_vadjustment()
        scroll_position = vadjustment.get_value() if vadjustment else 0.0

        matches = self._matching_conversations(folder, keep_id)
        self._replace_conversations(matches, keep_id)
        self._show_list_or_placeholder()
        self._update_reader()
        self._restore_scroll(vadjustment, scroll_position)

    def _matching_conversations(
        self, folder: Folder, keep_id: int | None
    ) -> list[Conversation]:
        """The folder's conversations, narrowed by the search box and filter."""
        query = self.search_entry.get_text().strip()
        matches = (
            self._db.search_conversations(folder.id, query)
            if query
            else self._db.conversations_in_folder(folder.id)
        )

        if not self.unread_button.get_active():
            return matches
        # Keep the conversation being read (keep_id) even once it's marked read,
        # so opening a mail here doesn't make it vanish under you; it drops out
        # on the next refresh when you move to another.
        return [item for item in matches if item.is_unread or item.id == keep_id]

    def _replace_conversations(
        self, matches: list[Conversation], keep_id: int | None
    ) -> None:
        """Swap in the new list, keeping keep_id selected if it survived.

        MultiSelection tracks positions while keep_id tracks the conversation
        itself, so the selection is cleared before the store is spliced and
        restored by identity afterwards. _selection_update_in_progress holds off
        the reader until both halves are done.
        """
        target = -1
        if keep_id is not None:
            target = next(
                (i for i, item in enumerate(matches) if item.id == keep_id), -1
            )

        store = self._conversation_store
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

    def _show_list_or_placeholder(self) -> None:
        if self._conversation_store.get_n_items() > 0:
            page = PAGE_LIST
        elif self._is_current_account_syncing():
            page = PAGE_LOADING
        else:
            page = PAGE_EMPTY
        self.conversation_stack.set_visible_child_name(page)

    @staticmethod
    def _restore_scroll(vadjustment: Gtk.Adjustment | None, position: float) -> None:
        """Put the scroll position back after the store was replaced.

        Deferred to an idle callback because the new contents have not been
        laid out yet, so get_upper() is still the old value.
        """
        if vadjustment is None or position <= 0:
            return

        def apply() -> bool:
            highest = vadjustment.get_upper() - vadjustment.get_page_size()
            vadjustment.set_value(min(position, highest))
            return False

        GLib.idle_add(apply)

    # Debounce keystrokes: query the database ~200ms after typing stops instead
    # of on every letter.
    def _on_search_changed(self, _entry: Gtk.SearchEntry) -> None:
        if self._search_timeout:
            GLib.source_remove(self._search_timeout)
        self._search_timeout = GLib.timeout_add(
            SEARCH_DEBOUNCE_MS, self._on_search_timeout
        )

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

    # Scrolling to the bottom pulls the next-older page for the current folder,
    # if the last sync said there's more to fetch.
    def _on_list_edge_reached(
        self, _scroller: Gtk.ScrolledWindow, pos: Gtk.PositionType
    ) -> None:
        if pos != Gtk.PositionType.BOTTOM:
            return
        folder = self._current_folder
        if folder is None or not self._is_online:
            return
        if self._is_current_account_syncing():
            return
        if not self._folders_with_more_mail.get(folder.id, False):
            return
        self._start_sync(
            self._accounts[folder.account_id],
            in_background=True,
            folder_name=folder.name,
            offset=self._loaded_counts.get(folder.id, 0),
        )

    # (position, n_items) come from the signal; we just re-read the current
    # selection, so the parameters are ignored.
    def _on_selection_changed(
        self, _selection: Gtk.MultiSelection, _position: int, _n_items: int
    ) -> None:
        if self._selection_update_in_progress:
            return
        self._update_reader()

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
            folder = self._current_folder
            row.bind(
                conversation,
                is_outgoing=folder is not None
                and mail_sync.is_outgoing_folder(folder.name),
            )

        factory.connect("setup", on_setup)
        factory.connect("bind", on_bind)
        return factory

    def _on_search_action(self, _action: Gio.SimpleAction, _param: object) -> None:
        self.search_bar.set_search_mode(not self.search_bar.get_search_mode())

    def _on_refresh_clicked(self, *_args: object) -> None:
        # Not in the background, so this is also the way past the sync cooldown
        # on the open folder.
        self._sync_all()

    # --- the reading pane: thread, bodies, and attachments ----------------

    def _update_reader(self) -> None:
        # The conversation store only exists once a mail view is loaded, and
        # there is nothing to read before then.
        if self._account is None:
            return
        selected = self._selected_conversations()
        if len(selected) != 1:
            self._rendered_id = None
            self._active_view = None
            self._set_reply_forward_enabled(False)
            self._set_mail_actions_enabled(bool(selected))
            if selected:
                self._update_action_buttons(selected)
            # Hiding the pane isn't enough: the views behind it keep their
            # WebViews, and each one holds a web process open.
            self._clear_thread()
            self.reader_stack.set_visible_child_name(PAGE_EMPTY)
            return

        conversation = selected[0]
        self._update_action_buttons(selected)
        self._set_reply_forward_enabled(False)

        # Already showing this thread (e.g. after a flag change) — don't rebuild.
        if conversation.id == self._rendered_id:
            view = self._active_view
            self._set_reply_forward_enabled(view is not None and view.raw is not None)
            self.reader_stack.set_visible_child_name("message")
            return

        self._rendered_id = conversation.id
        self._active_view = None
        self._render_thread(conversation)
        # Opening a conversation marks it read (like most mail clients).
        self._mark_conversation_read(conversation)

    # Reflect the selected conversations' state on the action buttons.
    def _update_action_buttons(self, selected: list[Conversation]) -> None:
        self._set_mail_actions_enabled(True)

        if any(conversation.is_unread for conversation in selected):
            self.mark_read_button.set_icon_name("mail-read-symbolic")
            self.mark_read_button.set_tooltip_text(_("Mark Read"))
        else:
            self.mark_read_button.set_icon_name("mail-unread-symbolic")
            self.mark_read_button.set_tooltip_text(_("Mark Unread"))

        if any(conversation.is_starred for conversation in selected):
            self.star_button.set_icon_name("starred-symbolic")
            self.star_button.set_tooltip_text(_("Unstar"))
        else:
            self.star_button.set_icon_name("non-starred-symbolic")
            self.star_button.set_tooltip_text(_("Star"))

    # Empty the reading pane, releasing each view's WebView as it goes.
    def _clear_thread(self) -> None:
        child = self.thread_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.thread_box.remove(child)
            if isinstance(child, MessageView):
                child.release()
            child = next_child

    # Build one MessageView per email, newest first. The newest starts expanded
    # (which loads its body); older ones load lazily when the user expands them.
    def _render_thread(self, conversation: Conversation) -> None:
        self.reader_subject.set_label(conversation.subject)
        self._clear_thread()

        should_load_remote_images = self._settings.get_boolean("load-remote-images")
        emails = list(reversed(conversation.emails))
        for index, mail in enumerate(emails):
            is_newest = index == 0
            view = MessageView(
                mail,
                on_load=self._load_body,
                on_save_attachment=self._save_attachment,
                on_open_attachment=self._open_attachment,
                on_rendered=self._on_newest_rendered if is_newest else None,
                on_unsubscribe=self._on_unsubscribe,
                is_expanded=is_newest,
                should_load_remote_images=should_load_remote_images,
                avatars=self._avatars,
            )
            self.thread_box.append(view)

        self.reader_stack.set_visible_child_name("message")

    def _on_newest_rendered(self, view: MessageView) -> None:
        if len(self._selected_conversations()) != 1:
            return
        self._active_view = view
        self._set_reply_forward_enabled(True)

    # Fetch one message's raw bytes for a MessageView: serve the cached copy if
    # we have it, else pull it over IMAP on a worker thread. (Marking read is
    # handled once per conversation in _mark_conversation_read.)
    def _load_body(self, mail: Email, callback: LoadCallback) -> None:
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

        request = BodyRequest(
            email_id=mail.id, uid=mail.server_id, folder_name=folder.name
        )
        thread = threading.Thread(
            target=self._body_worker,
            args=(account, request, callback),
            daemon=True,
        )
        thread.start()

    # Runs on the worker thread: network only, no Gtk/database access. Takes a
    # snapshot rather than the Email, which the main thread may mutate.
    def _body_worker(
        self,
        account: Account,
        request: BodyRequest,
        callback: LoadCallback,
    ) -> None:
        credential = secrets.credential_for(account)
        if credential is None:
            logger.warning("could not sign in to account %s", account.email)
            GLib.idle_add(
                self._deliver_body,
                callback,
                request.email_id,
                None,
                _("Could not sign in to this account."),
            )
            return

        try:
            raw = mail_sync.fetch_full_message(
                account, credential, request.folder_name, request.uid
            )
        except Exception as error:
            logger.exception(
                "could not fetch message uid %s from %s (account %s)",
                request.uid,
                request.folder_name,
                account.email,
            )
            _is_auth_failure, message = errors.classify(error, account.imap_host)
            GLib.idle_add(self._deliver_body, callback, request.email_id, None, message)
            return
        GLib.idle_add(self._deliver_body, callback, request.email_id, raw, None)

    # Back on the main thread: cache the body, then hand it to the MessageView.
    def _deliver_body(
        self,
        callback: LoadCallback,
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

        # A full disk or a read-only target fails here, not in save_finish. Left
        # unhandled the exception is swallowed by PyGObject and the user sees
        # neither the "Saved" toast nor any reason why.
        try:
            file.replace_contents(
                attachment.content, None, False, Gio.FileCreateFlags.NONE, None
            )
        except GLib.Error as error:
            logger.exception("could not save attachment to %s", file.get_path())
            self._toast(
                _("Couldn't save {name}: {msg}").format(
                    name=attachment.filename, msg=error.message
                )
            )
            return

        self._toast(_("Saved {name}.").format(name=attachment.filename))

    # Not /tmp: Flatpak gives the instance a private one, and the document
    # portal can't hand a file from there to another app -- the launch fails.
    def _open_attachment(self, attachment: Attachment) -> None:
        # Server-supplied name; "../../.bashrc" would otherwise escape the dir.
        name = Path(attachment.filename).name or "attachment"
        directory = Path(GLib.get_user_cache_dir()) / "attachments"
        shutil.rmtree(directory, ignore_errors=True)  # keep only the newest copy
        path = directory / name
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path.write_bytes(attachment.content)
        except OSError:
            logger.exception("could not write attachment %s to %s", name, path)
            self._toast(_("Couldn't open {name}.").format(name=attachment.filename))
            return

        launcher = Gtk.FileLauncher(file=Gio.File.new_for_path(str(path)))
        # Server-supplied bytes: make the portal ask which app, so one click
        # can't hand an arbitrary file straight to its default handler.
        launcher.set_always_ask(True)
        launcher.launch(self, None, self._on_launch_done, attachment.filename)

    def _on_launch_done(
        self,
        launcher: Gtk.FileLauncher,
        result: Gio.AsyncResult,
        filename: str,
    ) -> None:
        try:
            launcher.launch_finish(result)
        except GLib.Error as error:
            if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                return
            logger.warning("could not open attachment %s: %s", filename, error.message)
            self._toast(_("Couldn't open {name}.").format(name=filename))

    def _on_unsubscribe(self, target: Unsubscribe, on_done: Callable[[], None]) -> None:
        # Without a One-Click header the list wants a human at the other end,
        # and nothing is sent from here: hand it to the browser, or to the
        # composer if all the list published was a mailto. The banner stays --
        # the user has not unsubscribed yet, they have only been taken to it.
        if not target.is_one_click:
            if target.url:
                Gtk.UriLauncher(uri=target.url).launch(self, None, None)
            else:
                self.open_mailto(target.mailto)
            return

        # A one-click POST cannot be taken back, and its address comes out of a
        # stranger's header, so name where the request is going before making
        # it. Cancel stays the default: a stray Enter must not unsubscribe.
        dialog = Adw.AlertDialog(
            heading=_("Unsubscribe from this list?"),
            body=_("A request will be sent to {destination}.").format(
                destination=urlparse(target.url).hostname or target.url
            ),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("unsubscribe", _("Unsubscribe"))
        dialog.set_response_appearance("unsubscribe", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.connect("response", self._on_unsubscribe_response, target.url, on_done)
        dialog.present(self)

    def _on_unsubscribe_response(
        self,
        _dialog: Adw.AlertDialog,
        response: str,
        url: str,
        on_done: Callable[[], None],
    ) -> None:
        if response != "unsubscribe":
            return
        thread = threading.Thread(
            target=self._unsubscribe_worker, args=(url, on_done), daemon=True
        )
        thread.start()

    # Runs on the worker thread: network only, no Gtk/database access. Takes the
    # url as a plain string, and needs no credentials -- the list authenticates
    # the request by the opaque token already in the URL.
    def _unsubscribe_worker(self, url: str, on_done: Callable[[], None]) -> None:
        try:
            mail_sync.post_unsubscribe(url)
        except Exception:
            logger.exception(
                "could not unsubscribe via %s", urlparse(url).hostname or url
            )
            GLib.idle_add(self._on_unsubscribed, on_done, False)
            return
        GLib.idle_add(self._on_unsubscribed, on_done, True)

    # Back on the main thread. A failure leaves the banner up so the user can
    # try again; errors.classify() is for IMAP/SMTP and HTTPError subclasses
    # OSError, so it would answer "couldn't reach the mail server" here.
    def _on_unsubscribed(self, on_done: Callable[[], None], is_done: bool) -> bool:
        if not is_done:
            self._toast(_("Couldn't unsubscribe. The list didn't accept the request."))
            return False
        on_done()
        self._toast(_("Unsubscribed. It can take a few days to take effect."))
        return False

    # --- syncing, the Outbox, and the connection banner -------------------

    def _drain_outbox(self, account: Account) -> None:
        outbox = next(
            (
                folder
                for folder in self._db.folders_for_account(account.id)
                if folder.name == mail_sync.OUTBOX_FOLDER
            ),
            None,
        )
        if outbox is None:
            return

        pending = self._db.emails_in_folder(outbox.id)
        if not pending:
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
            args=(account, jobs),
            daemon=True,
        )
        thread.start()

    # Runs on the worker thread: network only, no Gtk/database access. Failures
    # travel back as the exception itself -- the mail stays in the Outbox, so
    # the user has to be told why rather than left believing it was sent.
    def _outbox_worker(
        self,
        account: Account,
        jobs: list[tuple[int, str, list[str], bytes]],
    ) -> None:
        credential = secrets.credential_for(account)
        if credential is None:
            logger.warning(
                "could not sign in to %s; the Outbox stays queued", account.email
            )
            return

        results: list[OutboxResult] = []
        for email_id, subject, recipients, raw in jobs:
            try:
                mail_sync.send_message(
                    account, credential, account.email, recipients, raw
                )
            except Exception as error:
                logger.exception(
                    "could not send queued message %d (%r) to %s via %s",
                    email_id,
                    subject,
                    ", ".join(recipients),
                    account.smtp_host,
                )
                results.append(OutboxResult(email_id, subject, raw, error))
            else:
                results.append(OutboxResult(email_id, subject, raw, None))
        GLib.idle_add(self._on_outbox_drained, account, results)

    # Back on the main thread: safe to touch the database and widgets. Files
    # under the account that sent, not the open one; dropping a stale one would
    # leave the mail in the Outbox to go out twice.
    def _on_outbox_drained(self, account: Account, results: list[OutboxResult]) -> bool:
        sent: Folder | None = None
        sent_count = 0
        for result in results:
            if result.error is not None:
                continue
            if sent is None:
                sent = mail_sync.sent_folder(self._db, account.id)
            # extract_recipients keeps only the addresses, so this row's initials
            # come from the address where a freshly composed one has a name.
            recipient = next(iter(compose.extract_recipients(result.raw)), "")
            row = self._db.save_email(
                sent.id,
                sender=account.email,
                sender_address=account.email,
                recipient=recipient,
                recipient_address=recipient,
                subject=result.subject,
                preview=result.subject,
                date=datetime.now().astimezone().isoformat(),
                is_unread=False,
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
            is_auth_failure, message = errors.classify(
                send_errors[0], account.smtp_host
            )
            self._show_connection_banner(
                ngettext(
                    "Couldn't send a queued message. {reason}",
                    "Couldn't send {n} queued messages. {reason}",
                    len(send_errors),
                ).format(n=len(send_errors), reason=message),
                self._retry_button_label(is_auth_failure),
            )
        return False

    def _start_sync(
        self,
        account: Account,
        in_background: bool = False,
        folder_name: str | None = None,
        offset: int = 0,
    ) -> None:
        # Don't pile background syncs (folder clicks, the poll timer) on top of
        # one already running for the same account.
        if in_background and account.id in self._syncing_account_ids:
            return

        self._set_syncing(account.id, True)
        if self.conversation_stack.get_visible_child_name() == PAGE_EMPTY:
            self.conversation_stack.set_visible_child_name(PAGE_LOADING)
        thread = threading.Thread(
            target=self._sync_worker,
            args=(account, folder_name, offset),
            daemon=True,
        )
        thread.start()

    # Send and fetch for every account. The open folder is the one folder worth
    # naming; the rest get their inbox.
    # ponytail: one thread per account at once, which is fine for a handful --
    # queue them if someone turns up with twenty.
    def _sync_all(self, in_background: bool = False) -> None:
        open_folder = self._current_folder
        for account in self._accounts.values():
            self._drain_outbox(account)
            folder_name = (
                open_folder.name
                if open_folder is not None and open_folder.account_id == account.id
                else None
            )
            self._start_sync(
                account, in_background=in_background, folder_name=folder_name
            )

    # Refresh on a timer using the configured interval (0 = manual only).
    def _reschedule_sync(self) -> None:
        if self._sync_timer_id:
            GLib.source_remove(self._sync_timer_id)
            self._sync_timer_id = 0
        minutes = self._settings.get_int(SETTING_SYNC_INTERVAL)
        if minutes > 0:
            self._sync_timer_id = GLib.timeout_add_seconds(
                minutes * SECONDS_PER_MINUTE, self._on_sync_tick
            )

    def _on_sync_tick(self) -> bool:
        if self._accounts and self._is_online:
            self._sync_all(in_background=True)
        return True

    # Runs on the worker thread: network only, no Gtk/database access.
    def _sync_worker(
        self,
        account: Account,
        folder_name: str | None,
        offset: int = 0,
    ) -> None:
        credential = secrets.credential_for(account)
        if credential is None:
            logger.warning("could not sign in to account %s", account.email)
            GLib.idle_add(
                self._on_sync_error,
                account,
                True,
                _("Could not sign in to this account."),
            )
            return

        try:
            result = mail_sync.fetch_mailbox(
                account, credential, folder_name, offset=offset
            )
        except Exception as error:
            logger.exception(
                "sync failed for %s on %s (folder %s, offset %d)",
                account.email,
                account.imap_host,
                folder_name or "inbox",
                offset,
            )
            is_auth_failure, message = errors.classify(error, account.imap_host)
            GLib.idle_add(self._on_sync_error, account, is_auth_failure, message)
            return
        GLib.idle_add(self._on_sync_done, account, result)

    # A sync still in flight when its account was deleted: filing its mail
    # would recreate the folders that went with it.
    def _is_stale(self, account: Account) -> bool:
        return account.id not in self._accounts

    # Back on the main thread: safe to touch the database and widgets.
    def _on_sync_done(self, account: Account, result: mail_sync.SyncResult) -> bool:
        # Before the staleness check: a dropped callback still has to release
        # the spinner and the Refresh button.
        self._set_syncing(account.id, False)
        if self._is_stale(account):
            return False

        # Remember the open conversation so a background poll doesn't yank it.
        selected = self._selected_conversation()
        keep_id = selected.id if selected is not None else None

        mailboxes = [
            mailbox
            for mailbox in result.folders
            if mailbox.name not in mail_sync.NAMESPACE_ROOTS
        ]

        # Shortest name first: a parent's name is a prefix of its children's, so
        # every parent is stored before a child looks it up.
        for mailbox in sorted(mailboxes, key=lambda box: len(box.name)):
            name, delimiter = mailbox.name, mailbox.delimiter
            selectable = imap_session.ATTR_NOSELECT not in mailbox.flags
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
            names = {box.name for box in mailboxes} | {mail_sync.OUTBOX_FOLDER}
            self._db.prune_folders(account.id, names)

        target = self._db.get_or_create_folder(
            account.id, result.folder, mail_sync.icon_for_folder(result.folder)
        )
        new_messages: list[mail_sync.MessageHeader] = []
        for message in result.messages:
            if (target.id, message.uid) in self._move_tombstones:
                continue
            added = self._db.save_incoming_email(target.id, message)
            if added and message.is_unread:
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
        self._db.save_contacts(
            [address for message in result.messages for address in message.addresses]
        )

        # Update paging state: track the deepest page loaded (max() so a
        # newest-page poll never forgets how far the user has scrolled back),
        # and offer "more" only while messages remain beyond it.
        reached = result.offset + len(result.messages)
        loaded = min(result.exists, max(self._loaded_counts.get(target.id, 0), reached))
        self._loaded_counts[target.id] = loaded
        self._folders_with_more_mail[target.id] = result.exists > loaded
        self._folder_sync_times[target.id] = time.monotonic()

        arrived_elsewhere = self._apply_unread_counts(account, result.unread_counts)

        self._reload_folders()
        self._refresh_conversations(keep_id=keep_id)
        self.connection_banner.set_revealed(False)

        self._notify_arrivals(account.id, new_messages, target.id, arrived_elsewhere)
        return False

    # Notification ids carry the account: every account syncs on the same tick,
    # and a repeated id replaces the notification already on screen.
    def _notify_arrivals(
        self,
        account_id: int,
        messages: list[mail_sync.MessageHeader],
        folder_id: int,
        arrived_elsewhere: dict[str, int],
    ) -> None:
        # Only nag about new mail when the user isn't already looking.
        if self.is_active():
            return
        if messages:
            self._notify_new_mail(account_id, messages, folder_id)
        if arrived_elsewhere:
            self._notify_unread_elsewhere(account_id, arrived_elsewhere)

    def _apply_unread_counts(
        self, account: Account, counts: dict[str, int]
    ) -> dict[str, int]:
        """Store the server's unread counts; return how many arrived per folder.

        A folder we have no earlier count for is only recorded -- on the first
        sync every count would otherwise read as new mail.
        """
        arrived: dict[str, int] = {}
        for name, count in counts.items():
            folder = self._db.get_folder_by_name(account.id, name)
            if folder is None:
                continue
            previous = self._remote_unread_counts.get(folder.id)
            if previous is not None and count > previous:
                arrived[mail_sync.display_name_for_folder(name)] = count - previous
            self._remote_unread_counts[folder.id] = count
        return arrived

    def _notify_unread_elsewhere(
        self, account_id: int, arrived: dict[str, int]
    ) -> None:
        """New mail in a folder we didn't fetch, so there are no headers to
        name -- only the count and where it landed."""
        if not self._settings.get_boolean("notifications"):
            return
        app = self.get_application()
        if app is None:
            return

        total = sum(arrived.values())
        notification = Gio.Notification.new(
            ngettext("{n} new message", "{n} new messages", total).format(n=total)
        )
        notification.set_body(", ".join(arrived))
        notification.set_default_action("app.focus-mail")
        app.send_notification(f"new-mail-elsewhere-{account_id}", notification)

    def _notify_new_mail(
        self, account_id: int, messages: list[mail_sync.MessageHeader], folder_id: int
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
            senders = ", ".join(dict.fromkeys(message.sender for message in messages))
            notification = Gio.Notification.new(
                _("{n} new messages").format(n=len(messages))
            )
            notification.set_body(senders)
            notification.set_default_action("app.focus-mail")

        app.send_notification(f"new-mail-{account_id}", notification)

    def _on_sync_error(
        self, account: Account, is_auth_failure: bool, message: str
    ) -> bool:
        self._set_syncing(account.id, False)
        # Another account's failure can leave the open folder on the spinner,
        # because _start_sync flips the stack for whichever account syncs.
        self._show_list_or_placeholder()
        if self._is_stale(account):
            return False
        self._show_connection_banner(message, self._retry_button_label(is_auth_failure))
        return False

    @staticmethod
    def _retry_button_label(is_auth_failure: bool) -> str:
        # Auth failures aren't worth a Retry button (same password); everything
        # else is a transient connection problem the user can retry.
        return "" if is_auth_failure else _("Retry")

    def _show_connection_banner(self, title: str, button_label: str = "") -> None:
        self.connection_banner.set_title(errors.linkify(title))
        self.connection_banner.set_button_label(button_label)
        self.connection_banner.set_revealed(True)

    def _show_offline_banner(self) -> None:
        self._show_connection_banner(
            _("You're offline. Postcard will reconnect when your connection returns.")
        )

    def _on_banner_retry(self, _banner: Adw.Banner) -> None:
        self.connection_banner.set_revealed(False)
        if self._accounts:
            self._sync_all()

    # network-changed fires on any change; act only on real online/offline flips.
    def _on_network_changed(
        self, _monitor: Gio.NetworkMonitor, is_available: bool
    ) -> None:
        if is_available == self._is_online:
            return
        self._is_online = is_available
        if not is_available:
            self._show_offline_banner()
            return
        self.connection_banner.set_revealed(False)
        if self._accounts:
            self._sync_all()

    def _notify_background(self) -> None:
        # Once per install, not once per close: the notice explains why the app
        # is still around the first time it happens, and is noise after that.
        if self._settings.get_boolean("background-notice-shown"):
            return

        self._settings.set_boolean("background-notice-shown", True)

        app = self.get_application()
        if app is None:
            return

        notification = Gio.Notification.new(_("Postcard is running in the background"))
        notification.set_body(_("It will keep checking for new mail. Quit to stop."))
        notification.set_default_action("app.focus-mail")
        app.send_notification("running-background", notification)

    def _set_syncing(self, account_id: int, is_syncing: bool) -> None:
        if is_syncing:
            self._syncing_account_ids.add(account_id)
        else:
            self._syncing_account_ids.discard(account_id)

        account_row = self._account_rows.get(account_id)
        if account_row is not None:
            account_row.set_syncing(is_syncing)

        self.refresh_button.set_sensitive(not self._syncing_account_ids)

    # Each account row spins on its own, but the conversation list only waits
    # on the one whose folder is open.
    def _is_current_account_syncing(self) -> bool:
        folder = self._current_folder
        return folder is not None and folder.account_id in self._syncing_account_ids
