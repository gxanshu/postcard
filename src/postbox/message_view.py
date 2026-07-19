from collections.abc import Callable

import gi

gi.require_version("WebKit", "6.0")

from gettext import gettext as _

from gi.repository import Adw, Gtk, Pango, WebKit

from .core.mime import message_parser
from .core.models.attachment import Attachment
from .core.models.email import Email

LoadCallback = Callable[[bytes | None, str | None], None]


class MessageView(Gtk.Box):
    __gtype_name__ = "PostboxMessageView"

    def __init__(
        self,
        email: Email,
        on_load: Callable[[Email, LoadCallback], None],
        on_save_attachment: Callable[[Attachment], None],
        on_rendered: Callable[["MessageView"], None] | None = None,
        expanded: bool = False,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("card")
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(12)
        self.set_margin_end(12)

        self._email = email
        self._on_load = on_load
        self._on_save_attachment = on_save_attachment
        self._on_rendered = on_rendered
        self._loaded = False
        self._loading = False
        self._placeholder: Gtk.Widget | None = None
        self._webview: WebKit.WebView | None = None
        self._html: str | None = None

        self.raw: bytes | None = None
        self.parsed: message_parser.ParsedMessage | None = None

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.append(Adw.Avatar(size=32, show_initials=True, text=email.sender))

        sender = Gtk.Label(
            label=email.sender, xalign=0, hexpand=True,
            ellipsize=Pango.EllipsizeMode.END,
        )
        sender.add_css_class("heading")
        header.append(sender)

        date = Gtk.Label(label=email.date, xalign=1)
        date.add_css_class("dim-label")
        header.append(date)

        self._toggle = Gtk.Button(child=header)
        self._toggle.add_css_class("flat")
        self._toggle.connect("clicked", self._on_toggle)
        self.append(self._toggle)

        self._body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._body.set_margin_start(12)
        self._body.set_margin_end(12)
        self._body.set_margin_bottom(12)

        self._revealer = Gtk.Revealer(child=self._body)
        self.append(self._revealer)

        if expanded:
            self._expand()

    def _on_toggle(self, _button: Gtk.Button) -> None:
        if self._revealer.get_reveal_child():
            self._revealer.set_reveal_child(False)
        else:
            self._expand()

    def _expand(self) -> None:
        self._revealer.set_reveal_child(True)
        if self._loaded or self._loading:
            return
        self._loading = True
        self._placeholder = Gtk.Label(label=_("Loading…"), margin_top=12)
        self._placeholder.add_css_class("dim-label")
        self._body.append(self._placeholder)
        self._on_load(self._email, self._on_raw)

    def _on_raw(self, raw: bytes | None, error: str | None) -> None:
        self._loading = False
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

        self._loaded = True
        self.raw = raw
        self.parsed = message_parser.parse_message(raw)

        if self.parsed.html_body:
            self._show_html(self.parsed.html_body)
        else:
            self._show_text(self.parsed.text_body or "")
        self._populate_attachments(self.parsed.attachments)

        if self._on_rendered is not None:
            self._on_rendered(self)

    def _show_text(self, text: str) -> None:
        label = Gtk.Label(label=text, xalign=0, yalign=0, wrap=True, selectable=True)
        self._body.append(label)

    def _show_html(self, html: str) -> None:
        self._html = html

        banner = Adw.Banner(
            title=_("Remote images are blocked to protect your privacy."),
            button_label=_("Show Images"),
            revealed=True,
        )
        banner.connect("button-clicked", self._on_show_images_clicked)
        self._body.append(banner)
        self._images_banner = banner

        webview = WebKit.WebView()
        webview.set_size_request(-1, 500)
        settings = webview.get_settings()
        settings.set_enable_javascript(False)
        settings.set_auto_load_images(False)
        webview.load_html(html, None)
        self._webview = webview
        self._body.append(webview)

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
                icon_name="document-save-symbolic", valign=Gtk.Align.CENTER
            )
            save_button.add_css_class("flat")
            save_button.connect("clicked", self._on_save_clicked, attachment)
            row.add_suffix(save_button)

            listbox.append(row)

    def _on_save_clicked(self, _button: Gtk.Button, attachment: Attachment) -> None:
        self._on_save_attachment(attachment)


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
