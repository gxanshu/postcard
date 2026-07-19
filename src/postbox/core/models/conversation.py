from gi.repository import GObject

from .email import Email


class Conversation(GObject.Object):
    __gtype_name__ = "PostboxConversation"

    def __init__(self, emails: list[Email]) -> None:
        super().__init__()
        self.emails: list[Email] = emails

    @property
    def id(self) -> int:
        return self.emails[0].conversation_id or self.emails[0].id

    @property
    def latest(self) -> Email:
        return self.emails[-1]

    @property
    def subject(self) -> str:
        return self.latest.subject

    @property
    def date(self) -> str:
        return self.latest.date

    @property
    def preview(self) -> str:
        return self.latest.preview

    @property
    def count(self) -> int:
        return len(self.emails)

    @property
    def unread(self) -> bool:
        return any(mail.unread for mail in self.emails)

    @property
    def participants(self) -> str:
        seen: list[str] = []
        for mail in self.emails:
            if mail.sender not in seen:
                seen.append(mail.sender)
        return ", ".join(seen)
