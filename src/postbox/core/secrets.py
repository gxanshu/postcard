import gi

gi.require_version("Secret", "1")

from gi.repository import Secret

_SCHEMA = Secret.Schema.new(
    "in.gxanshu.postbox.Account",
    Secret.SchemaFlags.NONE,
    {"account-id": Secret.SchemaAttributeType.INTEGER}
)

def store_password(account_id: int, password: str) -> None:
    Secret.password_store_sync(
        _SCHEMA,
        {"account-id": account_id},
        Secret.COLLECTION_DEFAULT,
        f"Postbox account {account_id}",
        password,
        None,
    )

def lookup_password(account_id: int) -> str | None:
    return Secret.password_lookup_sync(_SCHEMA, {"account-id": account_id}, None)


def clear_password(account_id: int) -> str | None:
    return Secret.password_clear_sync(_SCHEMA, {"account-id": account_id}, None)
