import logging
import threading
from collections.abc import Callable
from gettext import gettext as _

from gi.repository import Gdk, Gio, GLib, Gtk

from . import mail_sync
from .core import secrets
from .core.models.account import Account
from .core.models.conversation import Conversation
from .core.net import errors, imap_session
from .window_parts import MainWindowParts
from .window_types import MAIL_ACTIONS, REPLY_FORWARD_ACTIONS, FlagChange

logger = logging.getLogger(__name__)

# Move is the one action carrying a parameter (the destination folder name), so
# it is registered on its own wherever these are.
_MOVE_PARAM_TYPE = "s"


def _register(
    target: Gio.ActionMap,
    name: str,
    handler: Callable[..., None],
    param_type: str | None = None,
) -> None:
    """Add one activatable action to a window or a context menu's group."""
    action = Gio.SimpleAction.new(
        name, GLib.VariantType.new(param_type) if param_type else None
    )
    action.connect("activate", handler)
    target.add_action(action)


class MailActionsMixin(MainWindowParts):
    """Read/unread, star, and the row context menu."""

    def _setup_actions(self) -> None:
        for name, handler in (
            ("toggle-read", self._on_toggle_read),
            ("toggle-star", self._on_toggle_star),
            ("archive", self._on_archive),
            ("trash", self._on_trash),
            ("compose", self._on_compose_clicked),
            ("reply", self._on_reply_clicked),
            ("reply-all", self._on_reply_all_clicked),
            ("forward", self._on_forward_clicked),
            ("refresh", self._on_refresh_clicked),
            ("search", self._on_search_action),
        ):
            _register(self, name, handler)
        _register(self, "move", self._on_move, _MOVE_PARAM_TYPE)

        # Flag actions are Ctrl-modified so they don't fire while typing in search.
        app = self.get_application()
        if app is not None:
            for name, accels in (
                ("win.toggle-read", ["<ctrl>i"]),
                ("win.toggle-star", ["<ctrl>s"]),
                ("win.archive", ["<ctrl>e"]),
                ("win.trash", ["<ctrl>Delete"]),
                ("win.compose", ["<ctrl>n"]),
                ("win.reply", ["<ctrl>r"]),
                ("win.reply-all", ["<ctrl><shift>r"]),
                ("win.forward", ["<ctrl><shift>f"]),
                ("win.refresh", ["F5"]),
                ("win.search", ["<ctrl>f"]),
            ):
                app.set_accels_for_action(name, accels)

    def _set_actions_enabled(self, names: tuple[str, ...], is_enabled: bool) -> None:
        # Only Gio.SimpleAction can be toggled; the plain Gio.Action interface
        # exposes no setter, so anything else is skipped rather than crashing.
        for name in names:
            action = self.lookup_action(name)
            if isinstance(action, Gio.SimpleAction):
                action.set_enabled(is_enabled)

    def _set_mail_actions_enabled(self, is_enabled: bool) -> None:
        self._set_actions_enabled(MAIL_ACTIONS, is_enabled)
        self.move_button.set_sensitive(is_enabled)

    def _set_reply_forward_enabled(self, is_enabled: bool) -> None:
        self._set_actions_enabled(REPLY_FORWARD_ACTIONS, is_enabled)
        for button in (self.reply_button, self.reply_all_button, self.forward_button):
            button.set_sensitive(is_enabled)

    def _selected_conversations(self) -> list[Conversation]:
        # The store is built by _load_mail_view, so before an account is open
        # there is nothing selected rather than an error. Every mail action
        # funnels through here, which is why the guard belongs here.
        if self._account is None:
            return []
        # Ask the selection which positions are set rather than asking every
        # position whether it is selected: this runs several times per
        # selection change and per sync, over the whole folder.
        positions = self._selection.get_selection()
        selected = []
        for index in range(positions.get_size()):
            conversation = self._conversation_store.get_item(positions.get_nth(index))
            if isinstance(conversation, Conversation):
                selected.append(conversation)
        return selected

    def _selected_conversation(self) -> Conversation | None:
        selected = self._selected_conversations()
        return selected[0] if len(selected) == 1 else None

    def _on_toggle_read(self, _action: Gio.SimpleAction, _param: object) -> None:
        conversations = self._selected_conversations()
        if conversations:
            self._toggle_read(conversations)

    def _on_toggle_star(self, _action: Gio.SimpleAction, _param: object) -> None:
        conversations = self._selected_conversations()
        if conversations:
            self._toggle_flag(
                conversations,
                "is_starred",
                self._db.set_email_starred,
                imap_session.FLAG_FLAGGED,
            )

    def _toggle_read(self, conversations: list[Conversation]) -> None:
        self._toggle_flag(
            conversations,
            "is_unread",
            self._db.set_email_unread,
            imap_session.FLAG_SEEN,
            is_flag_inverted=True,
        )

    # Clear the is_unread flag for a whole conversation: locally, in the badges
    # and list, and on the server. Guarded rather than routed straight through
    # _toggle_flag, which on an already-read thread would flip it back to unread.
    def _mark_conversation_read(self, conversation: Conversation) -> None:
        if conversation.is_unread:
            self._toggle_read([conversation])

    def _toggle_flag(
        self,
        conversations: list[Conversation],
        field: str,
        save: Callable[[int, bool], None],
        flag: str,
        is_flag_inverted: bool = False,
    ) -> None:
        """Flip one boolean flag across a selection, locally and on the server.

        A mixed selection follows the aggregate command shown in the menu: if
        anything is unread, the whole selection is marked read. `field` names
        the Email attribute and `save` the Database setter that records it;
        is_flag_inverted covers \\Seen, which is the opposite of is_unread.
        """
        mails = [mail for conversation in conversations for mail in conversation.emails]
        value = not any(getattr(mail, field) for mail in mails)
        originals = {mail.id: getattr(mail, field) for mail in mails}

        def write(values: dict[int, bool]) -> None:
            for mail in mails:
                setattr(mail, field, values[mail.id])
                save(mail.id, values[mail.id])

        def revert() -> None:
            write(originals)
            self._after_flag_change(conversations)

        write(dict.fromkeys(originals, value))
        self._after_flag_change(conversations)
        uids = [
            uid
            for conversation in conversations
            for uid in mail_sync.server_uids(conversation)
        ]
        self._run_flag_worker(
            uids,
            flag,
            should_add=not value if is_flag_inverted else value,
            revert=revert,
        )

    # Update badges and the list after a flag change, keeping this
    # conversation selected (so the reader doesn't reload).
    def _after_flag_change(self, conversations: list[Conversation]) -> None:
        keep_id = conversations[0].id if len(conversations) == 1 else None
        self._reload_folders()
        self._refresh_conversations(keep_id=keep_id)

    def _run_flag_worker(
        self, uids: list[str], flag: str, should_add: bool, revert: Callable[[], None]
    ) -> None:
        account = self._account
        folder = self._current_folder
        if account is None or folder is None:
            return
        change = FlagChange(
            folder_name=folder.name,
            uids=tuple(uids),
            flag=flag,
            should_add=should_add,
        )
        thread = threading.Thread(
            target=self._flag_worker,
            args=(account, change, revert),
            daemon=True,
        )
        thread.start()

    # Runs on the worker thread: network only, no Gtk/database access.
    def _flag_worker(
        self,
        account: Account,
        change: FlagChange,
        revert: Callable[[], None],
    ) -> None:
        credential = secrets.credential_for(account)
        if credential is None:
            logger.warning("could not sign in to account %s", account.email)
            GLib.idle_add(self._on_flag_sign_in_failed, revert)
            return

        try:
            mail_sync.set_flag(
                account,
                credential,
                change.folder_name,
                change.uids,
                change.flag,
                change.should_add,
            )
        except Exception as error:
            logger.exception(
                "could not set %s on %d message(s) in %s (account %s)",
                change.flag,
                len(change.uids),
                change.folder_name,
                account.email,
            )
            GLib.idle_add(self._on_action_failed, revert, account.imap_host, error)

    def _on_flag_sign_in_failed(self, revert: Callable[[], None]) -> bool:
        # The row was already updated optimistically, so leaving it would show a
        # read/starred state the server never got, until the next sync undid it.
        revert()
        self._toast(_("Could not sign in to this account."))
        return False

    def _on_action_failed(
        self, revert: Callable[[], None], host: str, error: Exception
    ) -> bool:
        revert()
        _is_auth_failure, message = errors.classify(error, host)
        self._toast(_("Action failed: {msg}").format(msg=message))
        return False

    # Select an unselected right-clicked row, then pop up its actions menu.
    def _on_row_right_click(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
        item: Gtk.ListItem,
    ) -> None:
        position = item.get_position()
        if position == Gtk.INVALID_LIST_POSITION:
            return

        if not self._selection.is_selected(position):
            self._selection_update_in_progress = True
            try:
                self._selection.unselect_all()
                self._selection.select_item(position, True)
            finally:
                self._selection_update_in_progress = False
            self._update_reader()

        conversation = self._conversation_store.get_item(position)
        if not isinstance(conversation, Conversation):
            return

        # A gesture detached from its widget has nothing to anchor the popover
        # to; that can't happen while the row is on screen, but set_parent(None)
        # would abort in C rather than raise, so bail out here instead.
        row_widget = gesture.get_widget()
        if row_widget is None:
            return

        popover = Gtk.PopoverMenu()
        popover.insert_action_group("context", self._context_actions())
        popover.set_parent(row_widget)
        popover.set_menu_model(self._context_menu(conversation))
        popover.set_has_arrow(False)
        # GtkModelButton activates its action after closing the popover, so keep
        # the action hierarchy alive until activation has finished.
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))

        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.popup()

    def _context_actions(self) -> Gio.SimpleActionGroup:
        """The subset of the window's actions the row context menu offers."""
        actions = Gio.SimpleActionGroup()
        for name, handler in (
            ("toggle-read", self._on_toggle_read),
            ("toggle-star", self._on_toggle_star),
            ("archive", self._on_archive),
            ("trash", self._on_trash),
        ):
            _register(actions, name, handler)
        _register(actions, "move", self._on_move, _MOVE_PARAM_TYPE)
        return actions

    def _context_menu(self, conversation: Conversation) -> Gio.Menu:
        menu = Gio.Menu()

        selected = self._selected_conversations() or [conversation]

        flags = Gio.Menu()
        read = (
            _("Mark Read")
            if any(item.is_unread for item in selected)
            else _("Mark Unread")
        )
        star = _("Unstar") if any(item.is_starred for item in selected) else _("Star")
        flags.append(read, "context.toggle-read")
        flags.append(star, "context.toggle-star")
        menu.append_section(None, flags)

        actions = Gio.Menu()
        actions.append(_("Archive"), "context.archive")
        actions.append(_("Delete"), "context.trash")
        actions.append_submenu(_("Move to"), self._build_move_menu("context"))
        menu.append_section(None, actions)

        return menu
