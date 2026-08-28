from gi.repository import Adw, Gtk

from . import mail_sync
from .core.models.account import Account
from .core.models.folder import Folder


class FolderRow(Gtk.Box):
    __gtype_name__ = "PostcardFolderRow"

    def __init__(self) -> None:
        super().__init__(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )

        self._icon = Gtk.Image()
        self.append(self._icon)

        self._name_label = Gtk.Label(xalign=0, hexpand=True)
        self.append(self._name_label)

        # Adw.Spinner spins whenever it is visible -- no start/stop to track.
        # It lives in a fixed-size slot so an account row keeps the same width
        # between syncs; hiding the spinner itself shifts the whole sidebar.
        self._spinner_slot = Gtk.Box(width_request=16, height_request=16, visible=False)
        self._spinner = Adw.Spinner(visible=False)
        self._spinner_slot.append(self._spinner)
        self.append(self._spinner_slot)

        self._badge = Gtk.Label()
        self._badge.add_css_class("dim-label")
        self.append(self._badge)

        # Set only for account rows: clicking the address toggles its folders,
        # the same as the expander arrow next to it.
        self._expandable: Gtk.TreeListRow | None = None
        click = Gtk.GestureClick()
        click.connect("released", self._on_released)
        self.add_controller(click)

    def _on_released(
        self, _gesture: Gtk.GestureClick, n_press: int, _x: float, _y: float
    ) -> None:
        if self._expandable is None or n_press != 1:
            return
        self._expandable.set_expanded(not self._expandable.get_expanded())

    # Fill this row from a folder. Called every time the row is (re)used.
    def bind(self, folder: Folder, unread_count: int) -> None:
        self._icon.set_from_icon_name(folder.icon_name)
        self._name_label.set_label(
            mail_sync.display_name_for_folder(folder.name, folder.display_delimiter)
        )
        self._name_label.remove_css_class("heading")
        self._expandable = None
        self._spinner_slot.set_visible(False)
        self.set_syncing(False)
        self._badge.set_label(str(unread_count))
        self._badge.set_visible(unread_count > 0)

    # The same widget also draws the account headings the folders sit under, so
    # bind() above has to undo whatever this sets -- rows are recycled for both.
    def bind_account(
        self, account: Account, tree_list_row: Gtk.TreeListRow, is_syncing: bool
    ) -> None:
        self._expandable = tree_list_row
        self._icon.set_from_icon_name("avatar-default-symbolic")
        self._name_label.set_label(account.email)
        self._name_label.add_css_class("heading")
        self._spinner_slot.set_visible(True)
        self.set_syncing(is_syncing)
        self._badge.set_visible(False)

    # Only account rows use this; the window calls it as syncs start and finish.
    def set_syncing(self, is_syncing: bool) -> None:
        self._spinner.set_visible(is_syncing)
