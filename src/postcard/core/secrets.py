"""Where an account's sign-in comes from: the system keyring for an account
typed in by hand, GNOME Online Accounts for one imported from Settings."""

import gi

gi.require_version("Secret", "1")

from gi.repository import Secret

from . import goa
from .models.account import Account
from .net.auth import Credential

_SCHEMA = Secret.Schema.new(
    "in.gxanshu.postcard.Account",
    Secret.SchemaFlags.NONE,
    {"account-id": Secret.SchemaAttributeType.INTEGER},
)


def store_password(account_id: int, password: str) -> None:
    Secret.password_store_sync(
        _SCHEMA,
        {"account-id": str(account_id)},
        Secret.COLLECTION_DEFAULT,
        f"Postcard account {account_id}",
        password,
        None,
    )


def lookup_password(account_id: int) -> str | None:
    return Secret.password_lookup_sync(_SCHEMA, {"account-id": str(account_id)}, None)


def clear_password(account_id: int) -> bool:
    return Secret.password_clear_sync(_SCHEMA, {"account-id": str(account_id)}, None)


def credential_for(account: Account) -> Credential | None:
    """How to sign this account in, or None when we cannot.

    Called from the worker thread: both branches block on IPC, and the Online
    Accounts one can spend a network round trip refreshing an expired token.
    """
    if account.goa_id:
        return goa.credential(account.goa_id)

    password = lookup_password(account.id)
    return Credential(account.email, password) if password else None
