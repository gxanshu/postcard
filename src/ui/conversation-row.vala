public class Postbox.ConversationRow : Gtk.Box {

    private Adw.Avatar avatar;
    private Gtk.Label sender_label;
    private Gtk.Label date_label;
    private Gtk.Label subject_label;
    private Gtk.Label preview_label;
    private Gtk.Image unread_dot;

    public ConversationRow () {
        Object (orientation: Gtk.Orientation.HORIZONTAL, spacing: 12);
        this.margin_top = 8;
        this.margin_bottom = 8;
        this.margin_start = 12;
        this.margin_end = 12;

        // Left: initials avatar (no network fetching in Phase 2).
        avatar = new Adw.Avatar (40, null, true);
        this.append (avatar);

        // Right: a vertical stack of sender/date, subject, preview/dot.
        var text = new Gtk.Box (Gtk.Orientation.VERTICAL, 2) {
            hexpand = true
        };
        this.append (text);

        var top = new Gtk.Box (Gtk.Orientation.HORIZONTAL, 6);
        text.append (top);

        sender_label = new Gtk.Label (null) {
            xalign = 0,
            hexpand = true,
            ellipsize = Pango.EllipsizeMode.END
        };
        sender_label.add_css_class ("conversation-sender");
        top.append (sender_label);

        date_label = new Gtk.Label (null) { xalign = 1 };
        date_label.add_css_class ("dim-label");
        top.append (date_label);

        subject_label = new Gtk.Label (null) {
            xalign = 0,
            ellipsize = Pango.EllipsizeMode.END
        };
        text.append (subject_label);

        var bottom = new Gtk.Box (Gtk.Orientation.HORIZONTAL, 6);
        text.append (bottom);

        preview_label = new Gtk.Label (null) {
            xalign = 0,
            hexpand = true,
            ellipsize = Pango.EllipsizeMode.END
        };
        preview_label.add_css_class ("dim-label");
        bottom.append (preview_label);

        unread_dot = new Gtk.Image.from_icon_name ("media-record-symbolic") {
            pixel_size = 10,
            valign = Gtk.Align.CENTER
        };
        unread_dot.add_css_class ("unread-dot");
        bottom.append (unread_dot);
    }

    /* Fill this row from an email. Called every time the row is (re)used. */
    public void bind (FakeEmail email) {
        avatar.text = email.sender;
        sender_label.label = email.sender;
        date_label.label = email.date;
        subject_label.label = email.subject;
        preview_label.label = email.preview;
        unread_dot.visible = email.unread;
    }
}
