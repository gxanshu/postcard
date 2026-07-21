from gettext import gettext as _

from gi.repository import Adw, Gio, Gtk

# The sync-interval combo maps its row index to a minute value. 0 = manual.
INTERVAL_MINUTES = [0, 5, 15, 30, 60]


@Gtk.Template(resource_path="/in/gxanshu/postcard/ui/preferences-dialog.ui")
class PostcardPreferencesDialog(Adw.PreferencesDialog):
    __gtype_name__ = "PostcardPreferencesDialog"

    notifications_row: Adw.SwitchRow = Gtk.Template.Child()
    images_row: Adw.SwitchRow = Gtk.Template.Child()
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
        settings.bind("run-in-background", self.background_row, "active", flags)
        settings.bind("signature-enabled", self.signature_enabled_row, "active", flags)
        settings.bind(
            "signature-enabled",
            self.signature_view,
            "sensitive",
            Gio.SettingsBindFlags.GET,
        )

        self.interval_row.set_model(
            Gtk.StringList.new(
                [
                    _("Manually"),
                    _("Every 5 minutes"),
                    _("Every 15 minutes"),
                    _("Every 30 minutes"),
                    _("Every hour"),
                ]
            )
        )
        minutes = settings.get_int("sync-interval-minutes")
        index = INTERVAL_MINUTES.index(minutes) if minutes in INTERVAL_MINUTES else 2
        self.interval_row.set_selected(index)
        self.interval_row.connect("notify::selected", self._on_interval_changed)

        buffer = self.signature_view.get_buffer()
        buffer.set_text(settings.get_string("signature-text"))
        buffer.connect("changed", self._on_signature_changed)

    def _on_interval_changed(self, row: Adw.ComboRow, _param: object) -> None:
        self._settings.set_int(
            "sync-interval-minutes", INTERVAL_MINUTES[row.get_selected()]
        )

    def _on_signature_changed(self, buffer: Gtk.TextBuffer) -> None:
        start, end = buffer.get_bounds()
        self._settings.set_string("signature-text", buffer.get_text(start, end, False))
