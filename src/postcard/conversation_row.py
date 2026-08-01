# conversation_row.py
#
# SPDX-License-Identifier: GPL-3.0-or-later

from gi.repository import Adw, Gdk, Gtk, Pango

from .avatar_loader import AvatarLoader
from .core.models.conversation import Conversation


class ConversationRow(Gtk.Box):
    __gtype_name__ = "PostcardConversationRow"

    def __init__(self, avatars: AvatarLoader | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.set_margin_top(8)
        self.set_margin_bottom(8)
        self.set_margin_start(12)
        self.set_margin_end(12)

        self._avatars = avatars
        self._address = ""

        self._avatar = Adw.Avatar(size=40, show_initials=True)
        self.append(self._avatar)

        # Right: a vertical stack of sender/date, subject, preview/dot.
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.append(text)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        text.append(top)

        self._sender_label = Gtk.Label(
            xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END
        )
        self._sender_label.add_css_class("conversation-sender")
        top.append(self._sender_label)

        self._star = Gtk.Image.new_from_icon_name("starred-symbolic")
        self._star.set_pixel_size(12)
        top.append(self._star)

        self._date_label = Gtk.Label(xalign=1)
        self._date_label.add_css_class("dim-label")
        top.append(self._date_label)

        self._subject_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self._subject_label.add_css_class("conversation-subject")
        text.append(self._subject_label)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        text.append(bottom)

        self._preview_label = Gtk.Label(
            xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END
        )
        self._preview_label.add_css_class("dim-label")
        bottom.append(self._preview_label)

        self._unread_dot = Gtk.Image.new_from_icon_name("media-record-symbolic")
        self._unread_dot.set_pixel_size(10)
        self._unread_dot.set_valign(Gtk.Align.CENTER)
        self._unread_dot.add_css_class("unread-dot")
        bottom.append(self._unread_dot)

    # Fill this row from a conversation. Called every time the row is (re)used.
    def bind(self, conversation: Conversation) -> None:
        subject = conversation.subject
        if conversation.count > 1:
            subject = f"{subject}  ({conversation.count})"

        self._avatar.set_text(conversation.latest.sender)
        self._load_avatar(conversation.latest.sender_address)
        self._sender_label.set_label(conversation.participants)
        self._star.set_visible(conversation.is_starred)
        self._date_label.set_label(conversation.date)
        self._subject_label.set_label(subject)
        self._preview_label.set_label(conversation.preview)
        self._unread_dot.set_visible(conversation.is_unread)

        # CSS class names, not Python identifiers: they have to match the
        # selectors in style.css, which the is_ prefix does not apply to.
        if conversation.is_unread:
            self.add_css_class("unread")
        else:
            self.remove_css_class("unread")

    def _load_avatar(self, address: str) -> None:
        # Rows are recycled, so clear the old face and ignore a fetch that
        # lands after this row was rebound to someone else.
        self._address = address
        self._avatar.set_custom_image(None)
        if self._avatars is None:
            return

        def apply(texture: Gdk.Texture) -> None:
            if self._address == address:
                self._avatar.set_custom_image(texture)

        self._avatars.load(address, apply)
