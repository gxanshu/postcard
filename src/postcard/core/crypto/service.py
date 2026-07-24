from __future__ import annotations

from .backend import CryptoBackend
from .subprocess_backend import SubprocessBackend
from .types import SignatureEnvelope, SignatureResult, SignatureStatus


class CryptoService:
    def __init__(self, gnupg_home: str | None = None) -> None:
        self._backend: CryptoBackend | None = None
        self._gnupg_home = gnupg_home
        self._init_error: str | None = None

        try:
            self._backend = SubprocessBackend(gnupg_home=gnupg_home)
        except (FileNotFoundError, OSError) as exc:
            self._init_error = str(exc)

    @property
    def available(self) -> bool:
        return self._backend is not None

    def verify(self, envelope: SignatureEnvelope) -> SignatureResult:
        if self._backend is None:
            return SignatureResult(
                status=SignatureStatus.ERROR,
                message=self._init_error or "No crypto backend available",
            )
        try:
            return self._backend.verify(envelope)
        except Exception as exc:
            return SignatureResult(
                status=SignatureStatus.ERROR,
                message=str(exc),
            )
