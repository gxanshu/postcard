from gi.repository import Gtk

from . import mail_sync
from .core.models.folder import Folder


class FolderRow(Gtk.Box):
    __gtype_name__ = "PostcardFolderRow"

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(6)
        self.set_margin_end(6)

        self._icon = Gtk.Image()
        self.append(self._icon)

        self._name_label = Gtk.Label(xalign=0, hexpand=True)
        self.append(self._name_label)

        self._badge = Gtk.Label()
        self._badge.add_css_class("dim-label")
        self.append(self._badge)

    # Fill this row from a folder. Called every time the row is (re)used.
    def bind(self, folder: Folder, unread_count: int) -> None:
        self._icon.set_from_icon_name(folder.icon_name)
        self._name_label.set_label(
            mail_sync.display_name_for_folder(folder.name, folder.display_delimiter)
        )
        self._badge.set_label(str(unread_count))
        self._badge.set_visible(unread_count > 0)
