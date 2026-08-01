from gi.repository import Gio, GObject, Gtk

from . import mail_sync
from .core.models.folder import Folder
from .folder_row import FolderRow
from .window_parts import MainWindowParts


class FolderTreeMixin(MainWindowParts):
    """The folder sidebar: tree model, rows, and selection."""

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
        row_count = self._folder_tree_model.get_n_items()
        if row_count == 0:
            return
        target = 0
        for position in range(row_count):
            tree_row = self._folder_tree_model.get_item(position)
            if isinstance(tree_row, Gtk.TreeListRow):
                folder = tree_row.get_item()
                assert isinstance(folder, Folder)
                if mail_sync.role_for_folder(folder.name) == mail_sync.FolderRole.INBOX:
                    target = position
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
        if changed and self._is_online:
            self._start_sync(in_background=True, folder_name=folder.name)

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

        shape = [(folder.id, folder.parent_id) for folder in folders]
        if shape != self._folder_shape_pairs:
            self._folder_shape_pairs = shape
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
        row_count = self._folder_tree_model.get_n_items()
        if row_count == 0:
            return
        for position in range(row_count):
            tree_row = self._folder_tree_model.get_item(position)
            if isinstance(tree_row, Gtk.TreeListRow):
                folder = tree_row.get_item()
                if isinstance(folder, Folder) and folder.id == folder_id:
                    self._folder_selection.set_selected(position)
                    return
