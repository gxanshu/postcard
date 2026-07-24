from dataclasses import dataclass
from enum import StrEnum


class SignatureStatus(StrEnum):
    UNSIGNED = "unsigned"
    VALID = "valid"
    INVALID = "invalid"
    UNTRUSTED = "untrusted"
    EXPIRED = "expired"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass
class CertificateInfo:
    subject: str = ""
    email: str = ""
    issuer: str = ""
    fingerprint: str = ""
    not_before: str = ""
    not_after: str = ""
    certificate_pem: str = ""


@dataclass
class SignatureEnvelope:
    kind: str  # "detached" | "opaque"
    payload: bytes
    signature: bytes | None = None


@dataclass
class SignatureResult:
    status: SignatureStatus = SignatureStatus.UNSIGNED
    message: str = ""
    signer: CertificateInfo | None = None
