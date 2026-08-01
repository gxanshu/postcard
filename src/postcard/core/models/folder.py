from gi.repository import GObject


class Folder(GObject.Object):
    __gtype_name__ = "PostcardFolder"

    def __init__(
        self,
        *,
        id: int,
        account_id: int,
        name: str,
        icon_name: str,
        parent_id: int | None = None,
        delimiter: str = "/",
    ) -> None:
        super().__init__()
        self.id: int = id
        self.account_id: int = account_id
        self.name: str = name
        self.icon_name: str = icon_name
        self.parent_id: int | None = parent_id
        self.delimiter: str = delimiter

    # Strip to the leaf name only when there is a parent row to indent under.
    @property
    def display_delimiter(self) -> str | None:
        return self.delimiter if self.parent_id is not None else None
