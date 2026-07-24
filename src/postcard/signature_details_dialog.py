from datetime import datetime
from gettext import gettext as _

from gi.repository import Adw, Gtk

from .core.crypto.types import SignatureResult, SignatureStatus


def show_signature_details(
    parent: Gtk.Widget,
    result: SignatureResult,
) -> Adw.AlertDialog:
    heading = _("Signature Details")
    status_text = _status_label(result.status)
    reason = _status_reason(result)

    body_lines = [_("Signature status: {}").format(status_text)]
    if reason:
        body_lines.append(reason)
    if result.message:
        body_lines.append(result.message)

    dialog = Adw.AlertDialog(
        heading=heading,
        body="\n\n".join(body_lines),
    )

    extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=12)

    if result.signer:
        info = result.signer
        details = []
        if info.subject:
            details.append((_("Subject"), info.subject))
        if info.email:
            details.append((_("Email"), info.email))
        if info.issuer:
            details.append((_("Issuer"), info.issuer))
        if info.fingerprint:
            details.append((_("Fingerprint"), info.fingerprint))
        if info.not_before:
            details.append((_("Valid from"), _format_cert_date(info.not_before)))
        if info.not_after:
            details.append((_("Valid until"), _format_cert_date(info.not_after)))

        if details:
            grid = Gtk.Grid(row_spacing=6, column_spacing=12)

            for i, (label, value) in enumerate(details):
                name_label = Gtk.Label(
                    label=f"<b>{label}</b>",
                    use_markup=True,
                    xalign=1,
                )
                name_label.add_css_class("dim-label")
                grid.attach(name_label, 0, i, 1, 1)

                val_label = Gtk.Label(
                    label=value,
                    xalign=0,
                    wrap=True,
                    selectable=True,
                    hexpand=True,
                )
                grid.attach(val_label, 1, i, 1, 1)

            extra.append(grid)

        if info.certificate_pem:
            view_cert_button = Gtk.Button(label=_("View Certificate"))
            view_cert_button.connect(
                "clicked", _on_view_certificate_clicked, parent, info.certificate_pem
            )
            extra.append(view_cert_button)

    if extra.get_first_child() is not None:
        dialog.set_extra_child(extra)

    dialog.add_response("close", _("Close"))
    dialog.set_response_appearance("close", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("close")
    dialog.set_close_response("close")
    dialog.present(parent)
    return dialog


def _on_view_certificate_clicked(
    _button: Gtk.Button,
    parent: Gtk.Widget,
    pem: str,
) -> None:
    cert_dialog = Adw.AlertDialog(
        heading=_("Certificate (PEM)"),
    )

    text_view = Gtk.TextView(
        buffer=Gtk.TextBuffer(text=pem),
        editable=False,
        monospace=True,
        wrap_mode=Gtk.WrapMode.NONE,
        top_margin=12,
        bottom_margin=12,
        left_margin=12,
        right_margin=12,
    )
    scrolled = Gtk.ScrolledWindow(
        child=text_view,
        hexpand=True,
        vexpand=True,
        min_content_width=500,
        min_content_height=300,
        has_frame=True,
    )
    scrolled.add_css_class("card")

    cert_dialog.set_extra_child(scrolled)
    cert_dialog.add_response("close", _("Close"))
    cert_dialog.set_response_appearance("close", Adw.ResponseAppearance.SUGGESTED)
    cert_dialog.set_default_response("close")
    cert_dialog.set_close_response("close")
    cert_dialog.present(parent)


def _status_label(status: SignatureStatus) -> str:
    labels = {
        SignatureStatus.VALID: _("Valid"),
        SignatureStatus.INVALID: _("Invalid"),
        SignatureStatus.UNTRUSTED: _("Untrusted"),
        SignatureStatus.EXPIRED: _("Expired"),
        SignatureStatus.REVOKED: _("Revoked"),
        SignatureStatus.ERROR: _("Error"),
        SignatureStatus.UNSIGNED: _("Not signed"),
        SignatureStatus.UNKNOWN: _("Unknown"),
    }
    return labels.get(status, str(status.value))


def _status_reason(result: SignatureResult) -> str:
    match result.status:
        case SignatureStatus.VALID:
            return _(
                "The digital signature is valid and the certificate chain "
                "is trusted by the system certificate store."
            )
        case SignatureStatus.INVALID:
            return _(
                "The signature could not be verified. The message may have "
                "been altered or the signature does not match."
            )
        case SignatureStatus.UNTRUSTED:
            if result.message and "expired" not in result.message.lower():
                return _(
                    "The signature itself is mathematically correct, but the "
                    "sender's certificate is not trusted by the system "
                    "certificate store."
                )
            return _(
                "The signature itself is mathematically correct, but the "
                "sender's certificate could not be validated."
            )
        case SignatureStatus.EXPIRED:
            return _(
                "The signature itself is mathematically correct, but the "
                "sender's certificate has expired. Without a trusted "
                "timestamp (RFC 3161), the client cannot prove that the "
                "message was signed while the certificate was still valid."
            )
        case SignatureStatus.REVOKED:
            return _(
                "The signature itself is mathematically correct, but the "
                "sender's certificate has been revoked."
            )
        case SignatureStatus.ERROR:
            return _("An error occurred while trying to verify the signature.")
        case _:
            return ""


def _format_cert_date(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC")
