# fake_data.py
#
# Temporary hardcoded sample data so we can build and feel the three-pane UI
# before any networking or database exist. These are plain data objects and
# they get deleted in Phase 3.
#
# SPDX-License-Identifier: GPL-3.0-or-later

from gettext import gettext as _

from gi.repository import Gio, GObject


class FakeEmail(GObject.Object):
    __gtype_name__ = "PostboxFakeEmail"

    def __init__(
        self, sender: str, subject: str, preview: str, date: str, unread: bool
    ) -> None:
        super().__init__()
        self.sender: str = sender
        self.subject: str = subject
        self.preview: str = preview
        self.date: str = date
        self.unread: bool = unread


class FakeFolder(GObject.Object):
    __gtype_name__ = "PostboxFakeFolder"

    def __init__(self, name: str, icon_name: str) -> None:
        super().__init__()
        self.name: str = name
        self.icon_name: str = icon_name
        self.emails: Gio.ListStore = Gio.ListStore(item_type=FakeEmail)

    def add(
        self, sender: str, subject: str, preview: str, date: str, unread: bool
    ) -> None:
        self.emails.append(FakeEmail(sender, subject, preview, date, unread))


def fake_folders() -> Gio.ListStore:
    store = Gio.ListStore(item_type=FakeFolder)

    inbox = FakeFolder(_("inbox"), "mail-inbox-symbolic")
    inbox.add(
        "GNOME Foundation",
        "Welcome to GNOME 48",
        "Thanks for joining the community — here's what shipped this cycle and how to get involved.",
        "09:42",
        True,
    )
    inbox.add(
        "Migadu Support",
        "Your mailbox is ready",
        "Your new mailbox anshu@postbox.dev is provisioned. IMAP and SMTP settings are below.",
        "08:15",
        True,
    )
    inbox.add(
        "Ada Lovelace",
        "Re: Lunch on Thursday?",
        "Thursday works great for me. Let's meet at the usual place around noon.",
        "Yesterday",
        False,
    )
    inbox.add(
        "Vala Weekly",
        "Async/await, explained simply",
        "This week: a gentle walk through yield, plus a reader question about GListModel.",
        "Mon",
        False,
    )
    inbox.add(
        "Grace Hopper",
        "Debugging tips",
        "Attached are the notes from the talk. The bit about reading the reference, not copying it, applies here too.",
        "Sun",
        False,
    )
    store.append(inbox)

    starred = FakeFolder(_("Starred"), "starred-symbolic")
    starred.add(
        "Ada Lovelace",
        "Re: Lunch on Thursday?",
        "Thursday works great for me. Let's meet at the usual place around noon.",
        "Yesterday",
        False,
    )
    store.append(starred)

    sent = FakeFolder(_("Sent"), "mail-send-symbolic")
    sent.add(
        "Me",
        "Re: Your mailbox is ready",
        "Thanks! Got it working. Now building an email client to actually read it in.",
        "08:31",
        False,
    )
    store.append(sent)

    drafts = FakeFolder(_("Drafts"), "document-edit-symbolic")
    drafts.add("Me", "(no subject)", "Hey, just wanted to say —", "10:02", False)
    store.append(drafts)

    trash = FakeFolder(_("Trash"), "user-trash-symbolic")
    store.append(trash)

    return store
