from gi.repository import GObject


class Email(GObject.Object):
    __gtype_name__ = "PostcardEmail"

    def __init__(
        self,
        *,
        id: int,
        folder_id: int,
        # None until a sync assigns a UID -- a Sent copy saved locally right
        # after sending has no server-side counterpart yet.
        server_id: str | None,
        sender: str,
        subject: str,
        preview: str,
        date: str,
        is_unread: bool,
        is_starred: bool = False,
        message_id: str = "",
        in_reply_to: str = "",
        references: str = "",
        conversation_id: int | None = None,
        sender_address: str = "",
        # Who the message went to. Only outgoing folders show it, where every
        # message was sent by the account itself.
        recipient: str = "",
        recipient_address: str = "",
    ) -> None:
        super().__init__()
        self.id: int = id
        self.folder_id: int = folder_id
        self.server_id: str | None = server_id
        self.sender: str = sender
        self.sender_address: str = sender_address
        self.recipient: str = recipient
        self.recipient_address: str = recipient_address
        self.subject: str = subject
        self.preview: str = preview
        self.date: str = date
        self.is_unread: bool = is_unread
        self.is_starred: bool = is_starred
        self.message_id: str = message_id
        self.in_reply_to: str = in_reply_to
        self.references: str = references
        self.conversation_id: int | None = conversation_id
