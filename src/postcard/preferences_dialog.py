from gettext import gettext as _

from gi.repository import Adw, Gio, Gtk

# The sync-interval combo, in row order: the minute value stored in GSettings
# and the label shown for it. One list rather than two index-aligned ones, so a
# label can't drift from the value it describes. 0 = manual only.
SYNC_INTERVALS: list[tuple[int, str]] = [
    (0, _("Manually")),
    (5, _("Every 5 minutes")),
    (15, _("Every 15 minutes")),
    (30, _("Every 30 minutes")),
    (60, _("Every hour")),
]

# Used when the stored value isn't one of the offered intervals.
DEFAULT_SYNC_INTERVAL_MINUTES = 15

# GSettings key, also read by window.py to schedule the poll timer.
SETTING_SYNC_INTERVAL = "sync-interval-minutes"


@Gtk.Template(resource_path="/in/gxanshu/postcard/ui/preferences-dialog.ui")
class PostcardPreferencesDialog(Adw.PreferencesDialog):
    __gtype_name__ = "PostcardPreferencesDialog"

    notifications_row: Adw.SwitchRow = Gtk.Template.Child()
    images_row: Adw.SwitchRow = Gtk.Template.Child()
    avatars_row: Adw.SwitchRow = Gtk.Template.Child()
    background_row: Adw.SwitchRow = Gtk.Template.Child()
    interval_row: Adw.ComboRow = Gtk.Template.Child()
    signature_enabled_row: Adw.SwitchRow = Gtk.Template.Child()
    signature_view: Gtk.TextView = Gtk.Template.Child()

    def __init__(self, settings: Gio.Settings) -> None:
        super().__init__()
        self._settings = settings

        flags = Gio.SettingsBindFlags.DEFAULT
        settings.bind("notifications", self.notifications_row, "active", flags)
        settings.bind("load-remote-images", self.images_row, "active", flags)
        settings.bind("load-sender-avatars", self.avatars_row, "active", flags)
        settings.bind("run-in-background", self.background_row, "active", flags)
        settings.bind("signature-enabled", self.signature_enabled_row, "active", flags)
        settings.bind(
            "signature-enabled",
            self.signature_view,
            "sensitive",
            Gio.SettingsBindFlags.GET,
        )

        self.interval_row.set_model(
            Gtk.StringList.new([label for _minutes, label in SYNC_INTERVALS])
        )
        self.interval_row.set_selected(
            self._interval_index(settings.get_int(SETTING_SYNC_INTERVAL))
        )
        self.interval_row.connect("notify::selected", self._on_interval_changed)

        buffer = self.signature_view.get_buffer()
        buffer.set_text(settings.get_string("signature-text"))
        buffer.connect("changed", self._on_signature_changed)

    @staticmethod
    def _interval_index(minutes: int) -> int:
        """The combo row for a stored interval, falling back to the default."""
        offered = [value for value, _label in SYNC_INTERVALS]
        wanted = minutes if minutes in offered else DEFAULT_SYNC_INTERVAL_MINUTES
        return offered.index(wanted)

    def _on_interval_changed(self, row: Adw.ComboRow, _param: object) -> None:
        minutes, _label = SYNC_INTERVALS[row.get_selected()]
        self._settings.set_int(SETTING_SYNC_INTERVAL, minutes)

    def _on_signature_changed(self, buffer: Gtk.TextBuffer) -> None:
        start, end = buffer.get_bounds()
        self._settings.set_string("signature-text", buffer.get_text(start, end, False))
