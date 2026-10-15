import logging
import math
from collections.abc import Callable

import gi

gi.require_version("WebKit", "6.0")

from gettext import gettext as _

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango, WebKit

from . import mail_sync
from .avatar_loader import AvatarLoader
from .core.mime import message_parser
from .core.models.attachment import Attachment
from .core.models.email import Email

logger = logging.getLogger(__name__)

LoadCallback = Callable[[bytes | None, str | None], None]

# A mail body may name any scheme, and a registered handler will happily take
# file:, smb: or tel: from a stranger. Only these three are worth honouring.
EXTERNAL_SCHEMES = frozenset({"http", "https", "mailto"})

# The standard gutter, matching the 12px spacing used by folder and
# conversation rows. Adwaita's own margins are multiples of 6.
GUTTER = 12
SMALL_GUTTER = 6
# Content sits on the reader's own left edge, lined up with the subject above.
EDGE = 24
AVATAR_SIZE = 40

# A body is as tall as its content, so the reader pane is the only scroller.
# The root element is a block formatting context, so its box holds floats and
# the body's margins, unlike scrollHeight, which never drops below the viewport.
MEASURE_BODY = "document.documentElement.getBoundingClientRect().height"
# Used only if measuring fails, so the body never collapses to nothing.
FALLBACK_BODY_HEIGHT = 800
# Let a window drag settle before reloading a body reflowed by the new width.
RELOAD_DELAY_MS = 100

# An unrelated WebView costs its own web process: ~300 MB and up to 1.5 s to
# start. Related views share one, so every message body hangs off this anchor,
# which belongs to no conversation and so survives closing one. The composer's
# WebView stays unrelated on purpose -- it runs JavaScript and must not share a
# process with untrusted mail HTML.
_anchor: WebKit.WebView | None = None


def _ensure_anchor() -> WebKit.WebView:
    global _anchor
    if _anchor is not None:
        return _anchor

    # A mail body is rendered once and never navigated back to, so it needs
    # none of the caches WEB_BROWSER (the default) keeps.
    WebKit.WebContext.get_default().set_cache_model(WebKit.CacheModel.DOCUMENT_VIEWER)
    _anchor = WebKit.WebView()
    # The process starts on the first load, not on construction, so without
    # this the anchor holds nothing and dies with the last message view.
    _anchor.load_html("", None)
    return _anchor


def release_anchor() -> None:
    """Shut the shared web process down; the next message body starts a new one.

    Its ~300 MB is worth holding while the user is reading and not while the
    window is hidden or closed, which is where the window calls this. Dropping
    the last reference would leave the process up until the cyclic collector
    ran; terminating is deterministic.
    """
    global _anchor

    if _anchor is None:
        return
    _anchor.terminate_web_process()
    _anchor = None


class _BodyView(WebKit.WebView):
    """A WebView exactly as tall as the body it last measured.

    WebKit's own natural height is its scroll height, which never drops below
    the current viewport. Every load starts at 1px for the same reason: a body
    may size itself to the viewport (`min-height: 100vh`), and measured at its
    old height it could never shrink. 1px, not 0: a view laid out at zero
    height never paints, even once it grows.
    """

    __gtype_name__ = "PostcardBodyView"

    def __init__(self, on_width_changed: Callable[[int], None]) -> None:
        super().__init__(related_view=_ensure_anchor())
        self.body_height = 1
        self._width = 0
        self._on_width_changed = on_width_changed

    def do_measure(
        self, orientation: Gtk.Orientation, for_size: int
    ) -> tuple[int, int, int, int]:
        if orientation == Gtk.Orientation.HORIZONTAL:
            return WebKit.WebView.do_measure(self, orientation, for_size)
        return self.body_height, self.body_height, -1, -1

    # Here rather than on MessageView: GTK never calls size_allocate on a widget
    # with a layout manager, which a Gtk.Box is.
    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        WebKit.WebView.do_size_allocate(self, width, height, baseline)
        if width != self._width:
            self._width = width
            self._on_width_changed(width)


def _build_names(email: Email, delivered_to: str) -> Gtk.Box:
    names = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER
    )
    sender = Gtk.Label(label=email.sender, xalign=0, ellipsize=Pango.EllipsizeMode.END)
    sender.add_css_class("heading")
    names.append(sender)

    if email.sender_address:
        address = Gtk.Label(
            label=email.sender_address,
            xalign=0,
            ellipsize=Pango.EllipsizeMode.END,
            selectable=True,
        )
        address.add_css_class("caption")
        address.add_css_class("sender-address")
        names.append(address)

    if delivered_to:
        recipient = Gtk.Label(
            label=_("to {address}").format(address=delivered_to),
            xalign=0,
            ellipsize=Pango.EllipsizeMode.END,
        )
        recipient.add_css_class("caption")
        recipient.add_css_class("dim-label")
        names.append(recipient)
    return names


class MessageView(Gtk.Box):
    __gtype_name__ = "PostcardMessageView"

    def __init__(
        self,
        email: Email,
        on_load: Callable[[Email, LoadCallback], None],
        on_save_attachment: Callable[[Attachment], None],
        on_open_attachment: Callable[[Attachment], None],
        avatars: AvatarLoader,
        on_rendered: Callable[["MessageView"], None] | None = None,
        is_expanded: bool = False,
        should_load_remote_images: bool = False,
        is_12_hour_clock: bool = False,
        delivered_to: str = "",
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("message-view")

        self._email = email
        self._on_load = on_load
        self._on_save_attachment = on_save_attachment
        self._on_open_attachment = on_open_attachment
        self._on_rendered = on_rendered
        self._should_load_remote_images = should_load_remote_images
        self._is_12_hour_clock = is_12_hour_clock
        self._is_loaded = False
        self._is_loading = False
        self._is_released = False
        self._placeholder: Gtk.Widget | None = None
        self._webview: _BodyView | None = None
        self._html: str | None = None
        self._dark_handler_id = 0
        self._loaded_width = 0
        self._reload_id = 0

        self.raw: bytes | None = None
        self.parsed: message_parser.ParsedMessage | None = None

        header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=GUTTER,
            margin_top=GUTTER,
            margin_bottom=GUTTER,
            margin_start=EDGE,
            margin_end=EDGE,
        )
        avatar = Adw.Avatar(size=AVATAR_SIZE, show_initials=True, text=email.sender)
        header.append(avatar)
        if email.sender_address:
            avatars.load(email.sender_address, avatar.set_custom_image)

        header.append(_build_names(email, delivered_to))

        date = Gtk.Label(
            label=mail_sync.format_date(email.date, is_12_hour_clock),
            xalign=1,
            valign=Gtk.Align.CENTER,
        )
        date.add_css_class("dim-label")
        date.add_css_class("caption")
        header.append(date)

        toggle = Gtk.Button(child=header)
        toggle.add_css_class("flat")
        toggle.add_css_class("message-header")
        toggle.connect("clicked", self._on_toggle)
        self.append(toggle)

        self._body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=GUTTER)
        self._body.set_margin_start(EDGE)
        self._body.set_margin_end(EDGE)
        self._body.set_margin_bottom(EDGE)

        self._revealer = Gtk.Revealer(child=self._body)
        self.append(self._revealer)

        if is_expanded:
            self._expand()

    def _on_toggle(self, _button: Gtk.Button) -> None:
        if self._revealer.get_reveal_child():
            self._revealer.set_reveal_child(False)
        else:
            self._expand()

    def _expand(self) -> None:
        self._revealer.set_reveal_child(True)
        if self._is_loaded or self._is_loading:
            return
        self._is_loading = True
        self._placeholder = Gtk.Label(label=_("Loading…"), margin_top=GUTTER)
        self._placeholder.add_css_class("dim-label")
        self._body.append(self._placeholder)
        self._on_load(self._email, self._on_raw)

    def _on_raw(self, raw: bytes | None, error: str | None) -> None:
        if self._is_released:
            return

        self._is_loading = False
        if self._placeholder is not None:
            self._body.remove(self._placeholder)
            self._placeholder = None

        if raw is None:
            label = Gtk.Label(
                label=error or _("Couldn't load this message."), xalign=0, wrap=True
            )
            label.add_css_class("dim-label")
            self._body.append(label)
            return

        self._is_loaded = True
        self.raw = raw
        self.parsed = message_parser.parse_message(
            raw, is_12_hour_clock=self._is_12_hour_clock
        )

        self._show_details(self.parsed)

        if self.parsed.html_body:
            self._show_html(self.parsed.html_body)
        else:
            self._show_text(self.parsed.text_body or "")
        self._populate_attachments(self.parsed.attachments)

        if self._on_rendered is not None:
            self._on_rendered(self)

    # A collapsed Details section with the full From/To/Cc/Bcc/Date; the header
    # itself shows only the sender and their address.
    def _show_details(self, parsed: message_parser.ParsedMessage) -> None:
        grid = Gtk.Grid(row_spacing=4, column_spacing=GUTTER)
        grid.set_margin_bottom(SMALL_GUTTER)
        row = 0
        for label, value in (
            (_("From"), parsed.from_display),
            (_("To"), ", ".join(parsed.to)),
            (_("Cc"), ", ".join(parsed.cc)),
            (_("Bcc"), ", ".join(parsed.bcc)),
            (_("Date"), parsed.date),
        ):
            if not value:
                continue
            name = Gtk.Label(label=label, xalign=1, valign=Gtk.Align.START)
            name.add_css_class("dim-label")
            grid.attach(name, 0, row, 1, 1)
            content = Gtk.Label(
                label=value, xalign=0, wrap=True, selectable=True, hexpand=True
            )
            grid.attach(content, 1, row, 1, 1)
            row += 1

        if row == 0:
            return
        expander = Gtk.Expander(label=_("Details"))
        expander.set_child(grid)
        self._body.append(expander)

    def _show_text(self, text: str) -> None:
        label = Gtk.Label(label=text, xalign=0, yalign=0, wrap=True, selectable=True)
        self._body.append(label)

    def _show_html(self, html: str) -> None:
        self._html = html

        if not self._should_load_remote_images:
            banner = Adw.Banner(
                title=_("Remote images are blocked to protect your privacy."),
                button_label=_("Show Images"),
                revealed=True,
            )
            banner.connect("button-clicked", self._on_show_images_clicked)
            self._body.append(banner)
            self._images_banner = banner

        webview = _BodyView(self._on_body_width_changed)
        webview.connect("decide-policy", self._on_decide_policy)
        webview.connect("load-changed", self._on_load_changed)
        settings = webview.get_settings()
        # Our own height measurement needs JavaScript on; the message's never
        # runs. Markup off strips <script>, on* handlers and javascript: URLs at
        # parse time, and the sandbox CSP refuses any script that slips past.
        settings.set_enable_javascript(True)
        settings.set_enable_javascript_markup(False)
        # A message body needs none of these, and each one carries buffers.
        settings.set_enable_page_cache(False)
        settings.set_enable_media(False)
        settings.set_enable_webaudio(False)
        settings.set_enable_webgl(False)
        settings.set_enable_back_forward_navigation_gestures(False)
        # Transparent, so the reader's own surface shows through wherever the
        # recolored body leaves its page color out.
        webview.set_background_color(Gdk.RGBA(red=0, green=0, blue=0, alpha=0))
        self._webview = webview
        self._load_body()
        self._body.append(webview)
        self._dark_handler_id = Adw.StyleManager.get_default().connect(
            "notify::dark", self._on_dark_changed
        )

    def _on_body_width_changed(self, width: int) -> None:
        if width == self._loaded_width:
            return
        if self._reload_id:
            GLib.source_remove(self._reload_id)
        self._reload_id = GLib.timeout_add(RELOAD_DELAY_MS, self._on_reload)

    def _on_reload(self) -> bool:
        self._reload_id = 0
        self._load_body()
        return GLib.SOURCE_REMOVE

    # Load at 1px and measure once loaded (see `_BodyView`); a finished load is
    # also the one point where WebKit is sure to have laid out the new width.
    def _load_body(self) -> None:
        if self._webview is None:
            return
        self._loaded_width = self._webview.get_width()
        self._webview.body_height = 1
        self._webview.queue_resize()
        self._webview.load_html(self._sandboxed_html(), None)

    def _on_load_changed(
        self, _webview: WebKit.WebView, event: WebKit.LoadEvent
    ) -> None:
        if event == WebKit.LoadEvent.FINISHED and self._webview is not None:
            self._webview.evaluate_javascript(
                MEASURE_BODY, -1, None, None, None, self._on_body_measured
            )

    def _on_body_measured(self, webview: _BodyView, result: Gio.AsyncResult) -> None:
        if webview is not self._webview:
            return
        try:
            height = math.ceil(webview.evaluate_javascript_finish(result).to_double())
        except GLib.Error:
            logger.warning(
                "could not measure the body of message %s",
                self._email.message_id,
                exc_info=True,
            )
            height = FALLBACK_BODY_HEIGHT
        webview.body_height = height
        webview.queue_resize()

    def _on_dark_changed(self, _manager: Adw.StyleManager, _pspec: object) -> None:
        self._load_body()

    def _sandboxed_html(self) -> str:
        return message_parser.sandbox_html(
            self._html or "",
            are_remote_images_allowed=self._should_load_remote_images,
            is_dark=Adw.StyleManager.get_default().get_dark(),
        )

    # The webview only ever renders the message body: the one navigation it may
    # perform is the load_html document itself. A click goes to the browser, and
    # anything else the body asks for -- a meta refresh, a redirect, a target of
    # its own -- is refused outright.
    def _on_decide_policy(
        self,
        _webview: WebKit.WebView,
        decision: WebKit.PolicyDecision,
        _decision_type: WebKit.PolicyDecisionType,
    ) -> bool:
        # NAVIGATION_ACTION and NEW_WINDOW_ACTION are exactly the decisions
        # carrying a navigation action; RESPONSE ones aren't ours to handle.
        if not isinstance(decision, WebKit.NavigationPolicyDecision):
            return False

        action = decision.get_navigation_action()
        uri = action.get_request().get_uri() or ""
        scheme = uri.partition(":")[0].lower()

        if action.get_navigation_type() != WebKit.NavigationType.LINK_CLICKED:
            # load_html has no base URI, so its own document arrives as
            # about:blank -- or with no URI at all. Neither can leak anything.
            if scheme in ("about", ""):
                return False
            decision.ignore()
            logger.warning("blocked navigation from a message body to %s", uri)
            return True

        decision.ignore()
        if scheme not in EXTERNAL_SCHEMES:
            logger.warning(
                "blocked a message body link with scheme %r: %s", scheme, uri
            )
            return True

        root = self.get_root()
        Gtk.UriLauncher(uri=uri).launch(
            root if isinstance(root, Gtk.Window) else None, None, None
        )
        return True

    def _on_show_images_clicked(self, _banner: Adw.Banner) -> None:
        if self._webview is None or self._html is None:
            return
        self._should_load_remote_images = True
        self._images_banner.set_revealed(False)
        self._load_body()

    def _populate_attachments(self, attachments: list[Attachment]) -> None:
        if not attachments:
            return

        chips = Adw.WrapBox(child_spacing=SMALL_GUTTER, line_spacing=SMALL_GUTTER)
        for attachment in attachments:
            chips.append(self._attachment_chip(attachment))
        self._body.append(chips)

    # One chip per file: the name opens it in the default app, the arrow saves it.
    def _attachment_chip(self, attachment: Attachment) -> Gtk.Widget:
        # The extension, not the sender's MIME type, picks the icon: mailers
        # routinely label everything application/octet-stream.
        content_type, _uncertain = Gio.content_type_guess(attachment.filename, None)
        icon = Gtk.Image.new_from_gicon(Gio.content_type_get_icon(content_type))

        name = Gtk.Label(
            label=attachment.filename,
            ellipsize=Pango.EllipsizeMode.MIDDLE,
            max_width_chars=24,
        )
        size = Gtk.Label(label=GLib.format_size(attachment.size))
        size.add_css_class("dim-label")

        label = Gtk.Box(spacing=SMALL_GUTTER)
        label.append(icon)
        label.append(name)
        label.append(size)

        open_button = Gtk.Button(child=label, tooltip_text=attachment.filename)
        open_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Open %s") % attachment.filename]
        )
        open_button.connect(
            "clicked", lambda _button: self._on_open_attachment(attachment)
        )

        save_button = Gtk.Button(
            icon_name="folder-download-symbolic", tooltip_text=_("Save Attachment")
        )
        save_button.connect(
            "clicked", lambda _button: self._on_save_attachment(attachment)
        )

        chip = Gtk.Box()
        chip.add_css_class("linked")
        chip.append(open_button)
        chip.append(save_button)
        return chip

    def release(self) -> None:
        """Drop the body's widgets and bytes; the view is dead after this.

        The web process stays up -- it belongs to the anchor, which every other
        message shares, so `release_anchor` is what ends it. Disconnecting first
        breaks the decide-policy cycle that would otherwise hold this page's
        memory until the cyclic collector came round.
        """
        self._is_released = True
        if self._reload_id:
            GLib.source_remove(self._reload_id)
            self._reload_id = 0
        if self._dark_handler_id:
            Adw.StyleManager.get_default().disconnect(self._dark_handler_id)
            self._dark_handler_id = 0
        if self._webview is not None:
            self._webview.disconnect_by_func(self._on_decide_policy)
            self._webview.disconnect_by_func(self._on_load_changed)
            self._webview.unparent()
            self._webview = None
        self._html = None
        self.raw = None
        self.parsed = None
