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

from gi.repository import Adw, Gdk, Gio, GObject, Gtk

from . import fake_data
from .conversation_row import ConversationRow
from .fake_data import FakeEmail, FakeFolder


@Gtk.Template(resource_path="/in/gxanshu/postbox/ui/main-window.ui")
class PostboxMainWindow(Adw.ApplicationWindow):
    __gtype_name__ = "PostboxMainWindow"

    # These fields are filled in automatically from the widgets we named in
    # main-window.blp. The attribute name must match the id in the Blueprint
    # file exactly.
    folder_list: Gtk.ListBox = Gtk.Template.Child()
    conversation_list: Gtk.ListView = Gtk.Template.Child()
    reader_stack: Gtk.Stack = Gtk.Template.Child()
    reader_avatar: Adw.Avatar = Gtk.Template.Child()
    reader_sender: Gtk.Label = Gtk.Template.Child()
    reader_date: Gtk.Label = Gtk.Template.Child()
    reader_subject: Gtk.Label = Gtk.Template.Child()
    reader_body: Gtk.Label = Gtk.Template.Child()

    def __init__(self, app: Gtk.Application) -> None:
        super().__init__(application=app)

        self._folders: Gio.ListStore = fake_data.fake_folders()
        self._selection: Gtk.SingleSelection = Gtk.SingleSelection()

        self._load_styles()
        self._setup_folder_sidebar()
        self._setup_conversation_list()

        first = self.folder_list.get_row_at_index(0)
        if first is not None:
            self.folder_list.select_row(first)

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
        assert isinstance(folder, FakeFolder)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        box.append(Gtk.Image.new_from_icon_name(folder.icon_name))

        name = Gtk.Label(label=folder.name, xalign=0, hexpand=True)
        box.append(name)

        count = folder.emails.get_n_items()
        if count > 0:
            badge = Gtk.Label(label=str(count))
            badge.add_css_class("dim-label")
            box.append(badge)

        return box

    def _on_folder_selected(self, _list_box: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return

        folder = self._folders.get_item(row.get_index())
        assert isinstance(folder, FakeFolder)
        self._selection.set_model(folder.emails)
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
        email = self._selection.get_selected_item()
        if not isinstance(email, FakeEmail):
            self.reader_stack.set_visible_child_name("empty")
            return

        self.reader_avatar.set_text(email.sender)
        self.reader_sender.set_label(email.sender)
        self.reader_date.set_label(email.date)
        self.reader_subject.set_label(email.subject)
        self.reader_body.set_label(email.preview)

        # Opening a message marks it read (pretend, for now).
        email.unread = False

        self.reader_stack.set_visible_child_name("message")

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
            email = item.get_item()
            assert isinstance(row, ConversationRow)
            assert isinstance(email, FakeEmail)
            row.bind(email)

        factory.connect("setup", on_setup)
        factory.connect("bind", on_bind)
        return factory
