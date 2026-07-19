from gi.repository import GObject


class Attachment(GObject.Object):
    __gtype_name__ = "PostboxAttachment"

    def __init__(self, filename: str, mime_type: str, content: bytes) -> None:
        super().__init__()
        self.filename: str = filename
        self.mime_type: str = mime_type
        self.content: bytes = content

    @property
    def size(self) -> int:
        return len(self.content)
