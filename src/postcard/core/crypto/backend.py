from abc import ABC, abstractmethod

from .types import SignatureEnvelope, SignatureResult


class CryptoBackend(ABC):
    @abstractmethod
    def verify(self, envelope: SignatureEnvelope) -> SignatureResult: ...
