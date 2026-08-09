from collections.abc import Callable

import gi

gi.require_version("WebKit", "6.0")

from gettext import gettext as _

from gi.repository import Adw, Gtk, Pango, WebKit

from . import mail_sync
from .avatar_loader import AvatarLoader
from .core.mime import message_parser
from .core.models.attachment import Attachment
from .core.models.email import Email

LoadCallback = Callable[[bytes | None, str | None], None]

# The standard gutter, matching the 12px spacing used by folder and
# conversation rows. Adwaita's own margins are multiples of 6.
GUTTER = 12
SMALL_GUTTER = 6
# Content sits on the reader's own left edge, lined up with the subject above.
EDGE = 24
AVATAR_SIZE = 40

# Tall enough that most messages need no inner scrolling; the WebView can't
# report its content height until after layout, so this is a fixed guess.
BODY_HEIGHT = 800

# Attachment sizes. The last unit absorbs everything above it.
SIZE_UNITS = ("B", "KB", "MB", "GB")
BYTES_PER_UNIT = 1024

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


class MessageView(Gtk.Box):
    __gtype_name__ = "PostcardMessageView"

    def __init__(
        self,
        email: Email,
        on_load: Callable[[Email, LoadCallback], None],
        on_save_attachment: Callable[[Attachment], None],
        on_open_attachment: Callable[[Attachment], None],
        on_rendered: Callable[["MessageView"], None] | None = None,
        is_expanded: bool = False,
        should_load_remote_images: bool = False,
        avatars: AvatarLoader | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("message-view")

        self._email = email
        self._on_load = on_load
        self._on_save_attachment = on_save_attachment
        self._on_open_attachment = on_open_attachment
        self._on_rendered = on_rendered
        self._should_load_remote_images = should_load_remote_images
        self._is_loaded = False
        self._is_loading = False
        self._is_released = False
        self._placeholder: Gtk.Widget | None = None
        self._webview: WebKit.WebView | None = None
        self._html: str | None = None

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
        if avatars is not None and email.sender_address:
            avatars.load(email.sender_address, avatar.set_custom_image)

        names = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER
        )
        sender = Gtk.Label(
            label=email.sender, xalign=0, ellipsize=Pango.EllipsizeMode.END
        )
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
        header.append(names)

        date = Gtk.Label(
            label=mail_sync.format_date(email.date), xalign=1, valign=Gtk.Align.CENTER
        )
        date.add_css_class("dim-label")
        date.add_css_class("caption")
        header.append(date)

        self._toggle = Gtk.Button(child=header)
        self._toggle.add_css_class("flat")
        self._toggle.add_css_class("message-header")
        self._toggle.connect("clicked", self._on_toggle)
        self.append(self._toggle)

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
        self.parsed = message_parser.parse_message(raw)

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

        webview = WebKit.WebView(related_view=_ensure_anchor())
        webview.set_size_request(-1, BODY_HEIGHT)
        webview.connect("decide-policy", self._on_decide_policy)
        settings = webview.get_settings()
        settings.set_enable_javascript(False)
        settings.set_auto_load_images(self._should_load_remote_images)
        # A message body needs none of these, and each one carries buffers.
        settings.set_enable_page_cache(False)
        settings.set_enable_media(False)
        settings.set_enable_webaudio(False)
        settings.set_enable_webgl(False)
        settings.set_enable_back_forward_navigation_gestures(False)
        webview.load_html(html, None)
        self._webview = webview
        self._body.append(webview)

    # The webview only ever renders the message body; anything the user clicks
    # belongs in their browser, not in here.
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
        if action.get_navigation_type() != WebKit.NavigationType.LINK_CLICKED:
            return False

        decision.ignore()
        uri = action.get_request().get_uri()
        if uri:
            root = self.get_root()
            Gtk.UriLauncher(uri=uri).launch(
                root if isinstance(root, Gtk.Window) else None, None, None
            )
        return True

    def _on_show_images_clicked(self, _banner: Adw.Banner) -> None:
        if self._webview is None or self._html is None:
            return
        self._webview.get_settings().set_auto_load_images(True)
        self._images_banner.set_revealed(False)
        self._webview.load_html(self._html, None)

    def _populate_attachments(self, attachments: list[Attachment]) -> None:
        if not attachments:
            return

        heading = Gtk.Label(label=_("Attachments"), xalign=0)
        heading.add_css_class("heading")
        self._body.append(heading)

        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        self._body.append(listbox)

        for attachment in attachments:
            row = Adw.ActionRow(
                title=attachment.filename,
                subtitle=_human_size(attachment.size),
                activatable=True,
                tooltip_text=_("Open with the default app"),
            )
            row.add_prefix(Gtk.Image.new_from_icon_name("mail-attachment-symbolic"))
            row.connect("activated", self._on_open_clicked, attachment)

            save_button = Gtk.Button(
                icon_name="document-save-symbolic",
                valign=Gtk.Align.CENTER,
                tooltip_text=_("Save Attachment"),
            )
            save_button.add_css_class("flat")
            save_button.connect("clicked", self._on_save_clicked, attachment)
            row.add_suffix(save_button)

            listbox.append(row)

    def _on_save_clicked(self, _button: Gtk.Button, attachment: Attachment) -> None:
        self._on_save_attachment(attachment)

    def _on_open_clicked(self, _row: Adw.ActionRow, attachment: Attachment) -> None:
        self._on_open_attachment(attachment)

    def release(self) -> None:
        """Drop the body's widgets and bytes; the view is dead after this.

        The web process stays up -- it belongs to the anchor, which every other
        message shares, so `release_anchor` is what ends it. Disconnecting first
        breaks the decide-policy cycle that would otherwise hold this page's
        memory until the cyclic collector came round.
        """
        self._is_released = True
        if self._webview is not None:
            self._webview.disconnect_by_func(self._on_decide_policy)
            self._webview.unparent()
            self._webview = None
        self._html = None
        self.raw = None
        self.parsed = None


def _human_size(num_bytes: int) -> str:
    # The last unit has no larger one to promote to, so it absorbs whatever is
    # left rather than needing a separate fall-through branch to stay in sync.
    size = float(num_bytes)
    for unit in SIZE_UNITS[:-1]:
        if size < BYTES_PER_UNIT:
            return (
                f"{size:.0f} {unit}" if unit == SIZE_UNITS[0] else f"{size:.1f} {unit}"
            )
        size /= BYTES_PER_UNIT
    return f"{size:.1f} {SIZE_UNITS[-1]}"
