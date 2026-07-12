/* main-window.vala
 *
 * Copyright 2026 Anshu
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

[GtkTemplate (ui = "/in/gxanshu/postbox/ui/main-window.ui")]
public class Postbox.MainWindow : Adw.ApplicationWindow {

    // These fields are filled in automatically from the widgets we named in
    // main-window.blp. The name is the [GtkChild] filed must match the id
    // inthe Blueprint file exactly
    [GtkChild] private unowned Gtk.ListBox folder_list;
    [GtkChild] private unowned Gtk.ListView conversation_list;
    [GtkChild] private unowned Gtk.Stack reader_stack;
    [GtkChild] private unowned Adw.Avatar reader_avatar;
    [GtkChild] private unowned Gtk.Label reader_sender;
    [GtkChild] private unowned Gtk.Label reader_date;
    [GtkChild] private unowned Gtk.Label reader_subject;
    [GtkChild] private unowned Gtk.Label reader_body;

    private GLib.ListStore folders;
    private Gtk.SingleSelection selection;

    public MainWindow (Gtk.Application app) {
        Object (application: app);
    }

    // A tiny bit of app CSS: the accent-coloured unread dot and a bold sender
    // name. Loaded from a string so we don't need another resource file yet
    private void load_styles() {
        var provider = new Gtk.CssProvider();
        provider.load_from_string ("""
            .unread-dot { color: #3584e4; }
            .conversation-sender { font-weight: bold; }
        """);

        Gtk.StyleContext.add_provider_for_display (
            Gdk.Display.get_default (),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        );
    }

    // the folder list is small and fixed, so a Gtk.ListBox is the simplest
    // tool. bind_model() builds one row per folder and keep then in sync
    // with the store for free
    private void setup_folder_sidebar() {
        folders = FakeData.folders();
        folder_list.bind_model(folders, build_folder_row);
        folder_list.row_selected.connect(on_folder_selected);
    }

    private Gtk.Widget build_folder_row(GLib.Object item) {
        var folder = (FakeFolder) item;

        var box = new Gtk.Box(Gtk.Orientation.HORIZONTAL, 12) {
            margin_top = 6,
            margin_bottom = 6,
            margin_start = 6,
            margin_end = 6
        };
        box.append(new Gtk.Image.from_icon_name(folder.icon_name));

        var name = new Gtk.Label(folder.name) {xalign = 0, hexpand = true};
        box.append(name);

        var count = folder.emails.get_n_items();
        if (count > 0) {
            var badge = new Gtk.Label(count.to_string());
            badge.add_css_class("dim-label");
            box.append(badge);
        }

        return box;
    }

    private void on_folder_selected(Gtk.ListBoxRow? row) {
        if (row == null) {
            return;
        }

        var folder = (FakeFolder) folders.get_item(row.get_index());
        selection.model = folder.emails;
        selection.unselect_all();
        reader_stack.visible_child_name = "empty";
    }

    // Potentially thousands of row, so this uses the scalbe GTK4 patterns
    // a GListStore of data, a SingleSelection wrapper, and a factory that
    // recylces a handful of ConversationRow widget as you scroll
    private void setup_conversation_list() {
        selection = new Gtk.SingleSelection(null) {
            autoselect = false,
            can_unselect = true
        };

        // (position, n_items) comes from a the signal; we just re-read the current
        // selection, so the paramters are ignored
        selection.selection_changed.connect((position, n_items) => {
            update_reader();
        });

        conversation_list.model = selection;
        conversation_list.factory = build_conversation_factory();
    }

    /* --- Right pane: reader -------------------------------------------------- */
    private void update_reader () {
        var email = selection.selected_item as FakeEmail;
        if (email == null) {
            reader_stack.visible_child_name = "empty";
            return;
        }

        reader_avatar.text = email.sender;
        reader_sender.label = email.sender;
        reader_date.label = email.date;
        reader_subject.label = email.subject;
        reader_body.label = email.preview;

        // Opening a message marks it read (pretend, for now).
        email.unread = false;

        reader_stack.visible_child_name = "message";
    }

    private Gtk.SignalListItemFactory build_conversation_factory () {
        var factory = new Gtk.SignalListItemFactory ();

        // setup: build one empty widget. Runs rarely (only when GTK needs a
        // new reusable row), so it's fine to allocate here.
        factory.setup.connect ((object) => {
            var list_item = (Gtk.ListItem) object;
            list_item.child = new ConversationRow ();
        });

        // bind: fill an existing widget from its item. Runs often (every
        // scroll), so keep it cheap — just copy fields across.
        factory.bind.connect ((object) => {
            var list_item = (Gtk.ListItem) object;
            var row = (ConversationRow) list_item.child;
            row.bind ((FakeEmail) list_item.item);
        });

        return factory;
    }

    // construct runs after the template widgets exist, so its the right place
    // to fill the panes with date and connect singnals
    construct {
        load_styles();
        setup_folder_sidebar();
        setup_conversation_list();

        var first = folder_list.get_row_at_index(0);
        if (first != null) {
            folder_list.select_row(first);
        }
    }
}
