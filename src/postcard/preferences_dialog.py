import logging
from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, Gtk

from .core import autostart
from .window_types import SETTING_SYNC_INTERVAL

logger = logging.getLogger(__name__)

# The sync-interval combo, in row order: the minute value stored in GSettings
# and the label shown for it. One list rather than two index-aligned ones, so a
# label can't drift from the value it describes. 0 = manual only.
SYNC_INTERVALS: tuple[tuple[int, str], ...] = (
    (0, _("Manually")),
    (5, _("Every 5 minutes")),
    (15, _("Every 15 minutes")),
    (30, _("Every 30 minutes")),
    (60, _("Every hour")),
)

# Used when the stored value isn't one of the offered intervals.
DEFAULT_SYNC_INTERVAL_MINUTES = 15

# The Background portal owns the autostart .desktop file where it exists, and
# core.autostart writes it on the desktops that have no such portal. Neither can
# be read back, so the "start-at-login" key is our record of what was last set.
PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
BACKGROUND_INTERFACE = "org.freedesktop.portal.Background"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"
SETTING_START_AT_LOGIN = "start-at-login"


@Gtk.Template(resource_path="/in/gxanshu/postcard/ui/preferences-dialog.ui")
class PostcardPreferencesDialog(Adw.PreferencesDialog):
    __gtype_name__ = "PostcardPreferencesDialog"

    notifications_row: Adw.SwitchRow = Gtk.Template.Child()
    images_row: Adw.SwitchRow = Gtk.Template.Child()
    avatars_row: Adw.SwitchRow = Gtk.Template.Child()
    background_row: Adw.SwitchRow = Gtk.Template.Child()
    autostart_row: Adw.SwitchRow = Gtk.Template.Child()
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

        # Not bound to GSettings: the portal decides, and the key only
        # records its answer, so the row follows the reply rather than the click.
        self._autostart_subscription: int | None = None
        self.autostart_row.set_active(settings.get_boolean(SETTING_START_AT_LOGIN))
        self.autostart_row.connect("notify::active", self._on_autostart_toggled)

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

    def _on_autostart_toggled(self, row: Adw.SwitchRow, _param: object) -> None:
        is_wanted = row.get_active()
        # Also the exit for the row being put back after a refused request.
        if is_wanted == self._settings.get_boolean(SETTING_START_AT_LOGIN):
            return
        # A second request before the first answer would leave the row showing
        # the losing one, so the row waits until the portal has replied.
        row.set_sensitive(False)
        try:
            self._request_autostart(is_wanted)
        except GLib.Error:
            logger.info("no background portal, writing the autostart entry instead")
            self._write_autostart_entry(is_wanted)

    def _request_autostart(self, is_wanted: bool) -> None:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        # The reply comes back as a signal on a path derived from the token, so
        # subscribe before asking -- the portal may answer immediately.
        token = "postcard_" + GLib.uuid_string_random().replace("-", "_")
        sender = (bus.get_unique_name() or "").removeprefix(":").replace(".", "_")
        self._autostart_subscription = bus.signal_subscribe(
            PORTAL_NAME,
            REQUEST_INTERFACE,
            "Response",
            f"{PORTAL_PATH}/request/{sender}/{token}",
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_autostart_response,
            None,
        )
        options = {
            "handle_token": GLib.Variant("s", token),
            "reason": GLib.Variant(
                "s", _("Postcard checks for new mail after you log in.")
            ),
            "autostart": GLib.Variant("b", is_wanted),
            # Becomes the Exec line of the autostart entry the portal writes.
            "commandline": GLib.Variant("as", ["postcard", "--hidden"]),
        }
        bus.call(
            PORTAL_NAME,
            PORTAL_PATH,
            BACKGROUND_INTERFACE,
            "RequestBackground",
            GLib.Variant("(sa{sv})", ("", options)),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            self._on_autostart_requested,
            None,
        )

    def _on_autostart_requested(
        self, bus: Gio.DBusConnection, result: Gio.AsyncResult, _data: object
    ) -> None:
        try:
            bus.call_finish(result)
        except GLib.Error:
            logger.info("background portal refused, writing the autostart entry")
            self._write_autostart_entry(self.autostart_row.get_active())

    def _write_autostart_entry(self, is_wanted: bool) -> None:
        """Fallback for desktops whose portal has no Background backend."""
        directory = autostart.user_directory()
        try:
            autostart.set_entry(directory, is_enabled=is_wanted)
        except OSError:
            logger.exception("could not update the autostart entry in %s", directory)
            self._settle_autostart(
                self._settings.get_boolean(SETTING_START_AT_LOGIN),
                _("Could not change whether Postcard starts at login."),
            )
            return
        self._settle_autostart(is_wanted, "")

    def _on_autostart_response(
        self,
        _bus: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _interface: str,
        _signal: str,
        parameters: GLib.Variant,
        _data: object,
    ) -> None:
        response, results = parameters.unpack()
        is_wanted = self.autostart_row.get_active()
        # A non-zero response is a cancel or a failure: nothing was changed.
        is_enabled = (
            bool(results.get("autostart"))
            if response == 0
            else self._settings.get_boolean(SETTING_START_AT_LOGIN)
        )
        message = ""
        if is_enabled != is_wanted:
            logger.warning(
                "background portal did not set autostart to %s (response %d)",
                is_wanted,
                response,
            )
            message = _("Could not change whether Postcard starts at login.")
        self._settle_autostart(is_enabled, message)

    def _settle_autostart(self, is_enabled: bool, message: str) -> None:
        """Record what the portal actually did and match the row to it."""
        if self._autostart_subscription is not None:
            Gio.bus_get_sync(Gio.BusType.SESSION).signal_unsubscribe(
                self._autostart_subscription
            )
            self._autostart_subscription = None
        # Before the row, so the notify handler sees them agree and stops.
        self._settings.set_boolean(SETTING_START_AT_LOGIN, is_enabled)
        self.autostart_row.set_active(is_enabled)
        self.autostart_row.set_sensitive(True)
        if message:
            self.add_toast(Adw.Toast(title=message))
