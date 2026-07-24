from gettext import gettext as _

from gi.repository import Gtk

from .core.crypto.types import SignatureResult, SignatureStatus

_ICON_MAP = {
    SignatureStatus.VALID: "security-high-symbolic",
    SignatureStatus.UNTRUSTED: "security-medium-symbolic",
    SignatureStatus.EXPIRED: "appointment-missed-symbolic",
    SignatureStatus.REVOKED: "security-low-symbolic",
    SignatureStatus.INVALID: "security-low-symbolic",
    SignatureStatus.ERROR: "security-low-symbolic",
    SignatureStatus.UNKNOWN: "security-medium-symbolic",
}

_TOOLTIP_MAP = {
    SignatureStatus.VALID: _("Valid signature"),
    SignatureStatus.UNTRUSTED: _("Untrusted signature"),
    SignatureStatus.EXPIRED: _("Expired signature"),
    SignatureStatus.REVOKED: _("Revoked signature"),
    SignatureStatus.INVALID: _("Invalid signature"),
    SignatureStatus.ERROR: _("Signature verification failed"),
    SignatureStatus.UNKNOWN: _("Unknown signature status"),
}


class SignatureIndicator(Gtk.Button):
    __gtype_name__ = "PostcardSignatureIndicator"

    def __init__(self) -> None:
        super().__init__()
        self.add_css_class("flat")
        self._image = Gtk.Image()
        self.set_child(self._image)
        self._result: SignatureResult | None = None
        self.set_visible(False)

    def set_result(self, result: SignatureResult) -> None:
        self._result = result
        if result.status == SignatureStatus.UNSIGNED:
            self.set_visible(False)
            return

        icon_name = _ICON_MAP.get(result.status, "security-low-symbolic")
        self._image.set_from_icon_name(icon_name)

        tooltip = _TOOLTIP_MAP.get(
            result.status, _("Signature status: {}").format(result.status.value)
        )
        self.set_tooltip_text(tooltip)

        css_class = f"sig-{result.status.value}"
        for c in self.get_css_classes():
            if c.startswith("sig-"):
                self.remove_css_class(c)
        self.add_css_class(css_class)

        self.set_visible(True)
