from __future__ import annotations

from email.message import EmailMessage

from ..crypto.types import SignatureEnvelope

SMIME_SIGNED_PROTOCOLS = (
    "application/pkcs7-signature",
    "application/x-pkcs7-signature",
)

SMIME_OPAQUE_TYPES = (
    "application/pkcs7-mime",
    "application/x-pkcs7-mime",
)


def is_signed_message(msg: EmailMessage) -> bool:
    return _is_detached_signed(msg) or _is_opaque_signed(msg)


def extract_signature_envelope(msg: EmailMessage) -> SignatureEnvelope | None:
    if _is_detached_signed(msg):
        return _extract_detached(msg)
    if _is_opaque_signed(msg):
        return _extract_opaque(msg)
    return None


def _is_detached_signed(msg: EmailMessage) -> bool:
    if not msg.is_multipart():
        return False
    ct = msg.get_content_type()
    if ct != "multipart/signed":
        return False
    protocol = msg.get_param("protocol", None, "Content-Type")
    if protocol is None:
        return False
    protocol = protocol.strip().strip('"').lower()
    return protocol in SMIME_SIGNED_PROTOCOLS


def _is_opaque_signed(msg: EmailMessage) -> bool:
    ct = msg.get_content_type()
    if ct not in SMIME_OPAQUE_TYPES:
        return False
    smime_type = msg.get_param("smime-type", None, "Content-Type")
    if smime_type is None:
        return False
    smime_type = smime_type.strip().strip('"').lower()
    return smime_type == "signed-data"


def _extract_detached(msg: EmailMessage) -> SignatureEnvelope | None:
    parts = list(msg.iter_parts())
    if len(parts) != 2:
        return None

    signed_part = parts[0]
    sig_part = parts[1]

    sig_ct = sig_part.get_content_type().lower()
    if sig_ct not in SMIME_SIGNED_PROTOCOLS:
        return None

    payload = signed_part.as_bytes(unixfrom=False)
    signature = sig_part.get_content()
    if isinstance(signature, str):
        signature = signature.encode("utf-8")

    return SignatureEnvelope(
        kind="detached",
        payload=payload,
        signature=signature,
    )


def _extract_opaque(msg: EmailMessage) -> SignatureEnvelope | None:
    payload = msg.get_content()
    if isinstance(payload, str):
        payload = payload.encode("utf-8")

    return SignatureEnvelope(
        kind="opaque",
        payload=payload,
    )
