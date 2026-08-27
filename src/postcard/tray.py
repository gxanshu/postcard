"""The tray icon, spoken over D-Bus as a StatusNotifierItem.

GNOME ships no StatusNotifierWatcher, so registration never happens there and
the Background Apps menu stays the only handle on a hidden Postcard. KDE, and
bars like Waybar on Hyprland, do run a watcher, and this item gives them the
same handle. The protocol is hand-rolled with Gio.DBusConnection for the
reason core/goa.py is: the usual client library (libappindicator) is GTK 3
and not in the GNOME runtime.
"""

import logging
import math
from collections.abc import Callable
from gettext import gettext as _
from pathlib import Path

# From the platform, like gi: pycairo has no stub-only pip package, so pyright
# only resolves it on machines where the system copy ships its own stubs.
import cairo  # pyright: ignore[reportMissingImports]
from gi.repository import Gio, GLib

logger = logging.getLogger(__name__)

APP_ID = "in.gxanshu.postcard"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
ITEM_INTERFACE = "org.kde.StatusNotifierItem"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/StatusNotifierItem/Menu"

# Hosts hide Passive items on their own, and that is how this icon is shown
# and hidden: the watcher only forgets an item when its bus name dies, and
# this item lives under the application's own name.
STATUS_SHOWN = "Active"
STATUS_HIDDEN = "Passive"

ITEM_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/>
      <arg name="orientation" type="s" direction="in"/>
    </method>
    <signal name="NewIcon"/>
    <signal name="NewStatus">
      <arg name="status" type="s"/>
    </signal>
  </interface>
</node>
"""

# The right-click menu, in the com.canonical.dbusmenu flavour every
# StatusNotifier host expects.
MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="Status" type="s" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg name="events" type="a(isvu)" direction="in"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="updatesNeeded" type="ai" direction="out"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
  </interface>
</node>
"""

ROOT_ITEM_ID = 0
OPEN_ITEM_ID = 1
COMPOSE_ITEM_ID = 2
SYNC_ITEM_ID = 3
QUIT_ITEM_ID = 4
MENU_LABELS = {
    OPEN_ITEM_ID: _("Open Postcard"),
    COMPOSE_ITEM_ID: _("Compose"),
    SYNC_ITEM_ID: _("Refresh Inbox"),
    QUIT_ITEM_ID: _("Quit"),
}
# The menu never changes, so its revision never has to move.
MENU_REVISION = 1

ITEM_PROPERTIES = {
    "Category": GLib.Variant("s", "Communications"),
    "Id": GLib.Variant("s", APP_ID),
    "Title": GLib.Variant("s", "Postcard"),
    "ToolTip": GLib.Variant("(sa(iiay)ss)", (APP_ID, [], "Postcard", "")),
    "ItemIsMenu": GLib.Variant("b", False),
    "Menu": GLib.Variant("o", MENU_PATH),
}
MENU_PROPERTIES = {
    "Version": GLib.Variant("u", 3),
    "Status": GLib.Variant("s", "normal"),
}


ICON_SIZE = 64
BADGE_RADIUS = 19
BADGE_RED = (0.878, 0.106, 0.141)
BADGE_FONT_SIZES = {1: 26, 2: 22, 3: 17}
MAX_BADGE_COUNT = 99


def _icon_file() -> Path | None:
    for data_dir in GLib.get_system_data_dirs():
        path = Path(data_dir, "icons/hicolor/64x64/apps", f"{APP_ID}.png")
        if path.is_file():
            return path
    return None


def _badged_icon(count: int) -> tuple[int, int, bytes] | None:
    """The app icon with an unread bubble, or None to keep the themed icon."""
    icon_file = _icon_file()
    if icon_file is None:
        logger.warning("no %s.png on XDG_DATA_DIRS to draw the badge on", APP_ID)
        return None
    try:
        base = cairo.ImageSurface.create_from_png(str(icon_file))
    except (cairo.Error, OSError):
        logger.exception("could not read the app icon at %s", icon_file)
        return None

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, ICON_SIZE, ICON_SIZE)
    context = cairo.Context(surface)
    context.save()
    scale = ICON_SIZE / base.get_width()
    context.scale(scale, scale)
    context.set_source_surface(base, 0, 0)
    context.paint()
    context.restore()

    text = str(count) if count <= MAX_BADGE_COUNT else f"{MAX_BADGE_COUNT}+"
    center = ICON_SIZE - BADGE_RADIUS - 1
    context.arc(center, center, BADGE_RADIUS, 0, 2 * math.pi)
    context.set_source_rgb(*BADGE_RED)
    context.fill()
    context.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    context.set_font_size(BADGE_FONT_SIZES[len(text)])
    context.set_source_rgb(1, 1, 1)
    extents = context.text_extents(text)
    context.move_to(
        center - extents.width / 2 - extents.x_bearing,
        center - extents.height / 2 - extents.y_bearing,
    )
    context.show_text(text)
    surface.flush()
    return ICON_SIZE, ICON_SIZE, _network_order_argb(surface)


def _network_order_argb(surface: cairo.ImageSurface) -> bytes:
    """Repack pixels for the wire.

    Cairo keeps premultiplied ARGB in machine words; the tray protocol wants
    plain ARGB bytes in network order.
    """
    width, height = surface.get_width(), surface.get_height()
    stride = surface.get_stride()
    data = surface.get_data()
    argb = bytearray(width * height * 4)
    position = 0
    for row in range(height):
        start = row * stride
        pixels = memoryview(data)[start : start + width * 4].cast("I")
        for pixel in pixels:
            alpha = pixel >> 24
            red = (pixel >> 16) & 0xFF
            green = (pixel >> 8) & 0xFF
            blue = pixel & 0xFF
            if alpha not in (0, 255):
                red = red * 255 // alpha
                green = green * 255 // alpha
                blue = blue * 255 // alpha
            argb[position : position + 4] = (alpha, red, green, blue)
            position += 4
    return bytes(argb)


def _menu_layout() -> GLib.Variant:
    items = [
        GLib.Variant("(ia{sv}av)", (item_id, {"label": GLib.Variant("s", label)}, []))
        for item_id, label in MENU_LABELS.items()
    ]
    root = (ROOT_ITEM_ID, {"children-display": GLib.Variant("s", "submenu")}, items)
    return GLib.Variant("(u(ia{sv}av))", (MENU_REVISION, root))


class Tray:
    def __init__(
        self,
        *,
        on_open: Callable[[], None],
        on_compose: Callable[[], None],
        on_sync: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self._on_open = on_open
        self._menu_handlers = {
            OPEN_ITEM_ID: on_open,
            COMPOSE_ITEM_ID: on_compose,
            SYNC_ITEM_ID: on_sync,
            QUIT_ITEM_ID: on_quit,
        }
        self._bus: Gio.DBusConnection | None = None
        self._status = STATUS_HIDDEN
        self._unread = 0
        self._badge: tuple[int, int, bytes] | None = None

    def start(self) -> None:
        """Export the item, then register it with whatever watcher turns up."""
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION)
            item = Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0]
            menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
            bus.register_object(
                ITEM_PATH, item, self._on_item_call, self._item_property
            )
            bus.register_object(
                MENU_PATH, menu, self._on_menu_call, self._menu_property
            )
        except GLib.Error:
            logger.exception("could not put the tray icon on the session bus")
            return
        self._bus = bus
        Gio.bus_watch_name_on_connection(
            bus,
            WATCHER_NAME,
            Gio.BusNameWatcherFlags.NONE,
            self._on_watcher_appeared,
            None,
        )

    def set_shown(self, is_shown: bool) -> None:
        status = STATUS_SHOWN if is_shown else STATUS_HIDDEN
        if self._bus is None or status == self._status:
            return
        self._status = status
        self._bus.emit_signal(
            None, ITEM_PATH, ITEM_INTERFACE, "NewStatus", GLib.Variant("(s)", (status,))
        )

    def set_unread(self, count: int) -> None:
        if self._bus is None or count == self._unread:
            return
        self._unread = count
        self._badge = _badged_icon(count) if count else None
        self._bus.emit_signal(None, ITEM_PATH, ITEM_INTERFACE, "NewIcon", None)

    def _on_watcher_appeared(
        self, bus: Gio.DBusConnection, _name: str, _owner: str
    ) -> None:
        # Fires again after every bar restart, which needs a fresh registration.
        bus.call(
            WATCHER_NAME,
            WATCHER_PATH,
            WATCHER_NAME,
            "RegisterStatusNotifierItem",
            # The connection's unique name; a org.kde.StatusNotifierItem-* name
            # would need an --own-name the sandbox cannot wildcard.
            GLib.Variant("(s)", (bus.get_unique_name(),)),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            self._on_registered,
            None,
        )

    def _on_registered(
        self, bus: Gio.DBusConnection, result: Gio.AsyncResult, _data: object
    ) -> None:
        try:
            bus.call_finish(result)
        except GLib.Error:
            logger.exception("the status notifier watcher refused this item")

    def _on_item_call(
        self,
        _bus: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _interface: str,
        method: str,
        _parameters: GLib.Variant,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        # Middle click opens too. ContextMenu never arrives -- hosts render the
        # Menu property themselves -- and scrolling means nothing to a mailbox.
        if method in ("Activate", "SecondaryActivate"):
            self._on_open()
        invocation.return_value(None)

    def _item_property(
        self,
        _bus: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _interface: str,
        name: str,
    ) -> GLib.Variant:
        if name == "Status":
            return GLib.Variant("s", self._status)
        # A set IconName wins over IconPixmap in every host, so the name goes
        # blank while a badge is up.
        if name == "IconName":
            return GLib.Variant("s", "" if self._badge else APP_ID)
        if name == "IconPixmap":
            return GLib.Variant("a(iiay)", [self._badge] if self._badge else [])
        return ITEM_PROPERTIES[name]

    def _on_menu_call(
        self,
        _bus: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _interface: str,
        method: str,
        parameters: GLib.Variant,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        if method == "GetLayout":
            invocation.return_value(_menu_layout())
        elif method == "GetGroupProperties":
            wanted_ids, _names = parameters.unpack()
            rows = [
                (item_id, {"label": GLib.Variant("s", MENU_LABELS[item_id])})
                for item_id in wanted_ids
                if item_id in MENU_LABELS
            ]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (rows,)))
        elif method == "Event":
            item_id, event, _data, _timestamp = parameters.unpack()
            self._on_menu_event(item_id, event)
            invocation.return_value(None)
        elif method == "EventGroup":
            (events,) = parameters.unpack()
            for item_id, event, _data, _timestamp in events:
                self._on_menu_event(item_id, event)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))

    def _on_menu_event(self, item_id: int, event: str) -> None:
        handler = self._menu_handlers.get(item_id)
        if event == "clicked" and handler is not None:
            handler()

    def _menu_property(
        self,
        _bus: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _interface: str,
        name: str,
    ) -> GLib.Variant:
        return MENU_PROPERTIES[name]
