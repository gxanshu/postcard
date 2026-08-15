from dataclasses import dataclass

from .core.models.folder import Folder

# GSettings key for the poll interval. Named here rather than in
# preferences_dialog because the window schedules the timer from it and the
# dialog only writes it -- the window must not have to import a dialog.
SETTING_SYNC_INTERVAL = "sync-interval-minutes"

# Window action names, grouped by what enables and disables them together.
MAIL_ACTIONS = ("toggle-read", "toggle-star", "archive", "trash", "move")
REPLY_FORWARD_ACTIONS = ("reply", "reply-all", "forward")

# How long an archive/trash/move stays undoable before the real IMAP MOVE runs.
# The Undo toast is shown for this window, so the two have to agree.
MOVE_UNDO_MS = 5000

# Wait for typing to settle before running a search over FTS.
SEARCH_DEBOUNCE_MS = 200

# How long a folder counts as freshly synced. Opening one costs a connection,
# a login and a header fetch, which clicking between two folders would
# otherwise pay for on every visit.
# ponytail: a flat window, so a folder can read up to a minute stale and the
# refresh button syncs the inbox rather than the open folder. Point refresh at
# the current folder if that staleness ever shows.
FOLDER_SYNC_COOLDOWN_SECONDS = 60

SECONDS_PER_MINUTE = 60

# Gtk.Stack child names, matching the ids in main-window.blp.
PAGE_MAIL = "mail"
PAGE_NO_ACCOUNT = "no-account"
PAGE_EMPTY = "empty"
PAGE_LIST = "list"
PAGE_LOADING = "loading"


# The next two exist because of the threading model (see CLAUDE.md): a worker
# gets an immutable snapshot of what to do, never a live object the main thread
# might mutate underneath it. Frozen so that is enforced rather than hoped for
# -- which is also why uids is a tuple: frozen only blocks rebinding the field,
# so a list there would still be mutable from the main thread.


@dataclass(frozen=True, slots=True)
class FlagChange:
    """One IMAP flag edit to apply to a set of messages in one mailbox."""

    folder_name: str
    uids: tuple[str, ...]
    flag: str
    should_add: bool


@dataclass(frozen=True, slots=True)
class BodyRequest:
    """Which message body to fetch, as plain values rather than the Email."""

    email_id: int
    uid: str
    folder_name: str


@dataclass(frozen=True, slots=True)
class OutboxResult:
    """One attempted send from the Outbox. error is None when it went out."""

    email_id: int
    subject: str
    raw: bytes
    error: Exception | None


@dataclass(slots=True)
class PendingMove:
    """An archive/trash/move applied locally but not yet run on the server.

    email_ids, uids, originals and tombstones are index-aligned: a move that
    fails part-way reports how many succeeded, and the tail is restored by
    slicing all four at the same point.

    timeout_id is the GLib source that commits the move once the undo window
    closes; cancelling the move means removing that source.
    """

    email_ids: list[int]
    uids: list[str]
    originals: list[tuple[int, int, str]]
    source: Folder
    dest: Folder
    tombstones: list[tuple[int, str]]
    timeout_id: int
