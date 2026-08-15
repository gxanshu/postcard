from typing import NamedTuple

MECHANISM_LOGIN = "login"
MECHANISM_XOAUTH2 = "xoauth2"


class Credential(NamedTuple):
    user: str
    secret: str
    mechanism: str = MECHANISM_LOGIN

    def __repr__(self) -> str:
        # These travel as positional worker-thread arguments, so the default
        # tuple repr would put a password or access token in any traceback that
        # prints locals.
        return (
            f"Credential(user={self.user!r}, secret=..., mechanism={self.mechanism!r})"
        )


def xoauth2_response(user: str, access_token: str) -> str:
    """The SASL XOAUTH2 initial response (RFC 7628), before base64."""
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01"
