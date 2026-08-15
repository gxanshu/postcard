from gi.repository import Gdk, Gio, GLib, GObject, Gtk

from . import mail_sync
from .conversation_row import ConversationRow
from .core.models.conversation import Conversation
from .core.models.folder import Folder
from .window_parts import MainWindowParts
from .window_types import (
    PAGE_EMPTY,
    PAGE_LIST,
    PAGE_LOADING,
    SEARCH_DEBOUNCE_MS,
)


class ConversationListMixin(MainWindowParts):
    """The conversation list: contents, search, and paging."""

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
        elif self._is_syncing:
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
        if folder is None or self._is_syncing or not self._is_online:
            return
        if not self._folders_with_more_mail.get(folder.id, False):
            return
        self._start_sync(
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
        self._drain_outbox()
        self._start_sync()
