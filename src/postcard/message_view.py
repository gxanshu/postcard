import weakref
from collections.abc import Callable

import gi

gi.require_version("WebKit", "6.0")

from gettext import gettext as _

from gi.repository import Adw, Gdk, Gtk, Pango, WebKit

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

# WebKit gives every *unrelated* WebView its own web process, and one costs
# ~300 MB -- a five-message thread with each message expanded would run five of
# them. Related views share a single process, so every message body is created
# against the last one still alive. Weak so that releasing the views actually
# lets the process exit. The composer's WebView is deliberately not in here: it
# runs JavaScript and must not share a process with untrusted mail HTML.
_last_webview: weakref.ReferenceType[WebKit.WebView] | None = None


class MessageView(Gtk.Box):
    __gtype_name__ = "PostcardMessageView"

    def __init__(
        self,
        email: Email,
        on_load: Callable[[Email, LoadCallback], None],
        on_save_attachment: Callable[[Attachment], None],
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
            )
            address.add_css_class("caption")
            address.add_css_class("sender-address")
            names.append(address)
        header.append(names)

        date = Gtk.Label(label=email.date, xalign=1, valign=Gtk.Align.CENTER)
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

        global _last_webview

        # WEB_BROWSER, the default, keeps the largest memory and disk caches of
        # the three models plus a page cache for going back. A mail body is
        # rendered once and never navigated back to.
        WebKit.WebContext.get_default().set_cache_model(
            WebKit.CacheModel.DOCUMENT_VIEWER
        )

        related = _last_webview() if _last_webview is not None else None
        webview = WebKit.WebView(related_view=related) if related else WebKit.WebView()
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
        # Clearing the accelerated surface avoids a black frame before WebKit
        # paints; the GTK class supplies the white canvas expected by email HTML.
        webview.set_background_color(Gdk.RGBA(red=0, green=0, blue=0, alpha=0))
        webview.add_css_class("message-html")
        webview.load_html(html, None)
        self._webview = webview
        _last_webview = weakref.ref(webview)
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
                title=attachment.filename, subtitle=_human_size(attachment.size)
            )
            row.add_prefix(Gtk.Image.new_from_icon_name("mail-attachment-symbolic"))

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

    def release(self) -> None:
        """Tear the body down so its web process exits. The view is dead after this.

        Only the reader calls it, and only on views it is discarding as a set: a
        WebView left parented keeps a ~300 MB web process alive however long the
        reading pane sits empty. `parsed` goes too -- its attachments hold every
        decoded attachment's bytes.

        Dropping the last reference ought to be enough, but measurement says
        otherwise: the process outlives the widget, because the decide-policy
        closure holds this view and the view's own wrapper then needs the cyclic
        collector to notice. Terminating is deterministic instead of hoping. It
        also kills any view sharing the process (see _last_webview), which is
        safe only because the reader releases a whole thread at once.
        """
        global _last_webview

        self._is_released = True
        if self._webview is not None:
            if _last_webview is not None and _last_webview() is self._webview:
                _last_webview = None
            self._webview.disconnect_by_func(self._on_decide_policy)
            self._webview.terminate_web_process()
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
