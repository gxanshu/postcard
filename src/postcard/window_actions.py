# window_actions.py
#
# Window actions: read/unread, star, and the row context menu.
#
# A mixin on PostcardMainWindow: see window_parts.py for the state and the
# sibling methods it relies on.
#
# SPDX-License-Identifier: GPL-3.0-or-later

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
            ("forward", self._on_forward_clicked),
            ("refresh", self._on_refresh_clicked),
            ("search", self._on_search_action),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

        move = Gio.SimpleAction.new("move", GLib.VariantType.new("s"))
        move.connect("activate", self._on_move)
        self.add_action(move)

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

    def _selected_conversations(self) -> list[Conversation]:
        # The store is built by _load_mail_view, so before an account is open
        # there is nothing selected rather than an error. Every mail action
        # funnels through here, which is why the guard belongs here.
        if self._account is None:
            return []
        selected = []
        for position in range(self._conversation_store.get_n_items()):
            if not self._selection.is_selected(position):
                continue
            conversation = self._conversation_store.get_item(position)
            if isinstance(conversation, Conversation):
                selected.append(conversation)
        return selected

    def _selected_conversation(self) -> Conversation | None:
        selected = self._selected_conversations()
        return selected[0] if len(selected) == 1 else None

    def _on_toggle_read(self, _action: Gio.SimpleAction, _param: object) -> None:
        conversations = self._selected_conversations()
        if not conversations:
            return
        self._toggle_read_many(conversations)

    def _toggle_read_many(self, conversations: list[Conversation]) -> None:
        # A mixed selection follows the aggregate command shown in the menu:
        # if anything is is_unread, mark the whole selection read.
        is_unread = not any(conversation.is_unread for conversation in conversations)
        originals = {
            mail.id: mail.is_unread
            for conversation in conversations
            for mail in conversation.emails
        }
        for conversation in conversations:
            for mail in conversation.emails:
                mail.is_unread = is_unread
                if is_unread:
                    self._db.mark_email_unread(mail.id)
                else:
                    self._db.mark_email_read(mail.id)

        def revert() -> None:
            for conversation in conversations:
                for mail in conversation.emails:
                    old_unread = originals[mail.id]
                    mail.is_unread = old_unread
                    if old_unread:
                        self._db.mark_email_unread(mail.id)
                    else:
                        self._db.mark_email_read(mail.id)
            self._after_flag_change(conversations)

        self._after_flag_change(conversations)
        uids = [
            uid
            for conversation in conversations
            for uid in mail_sync.server_uids(conversation)
        ]
        self._run_flag_worker(
            uids, imap_session.FLAG_SEEN, should_add=not is_unread, revert=revert
        )

    def _on_toggle_star(self, _action: Gio.SimpleAction, _param: object) -> None:
        conversations = self._selected_conversations()
        if not conversations:
            return
        self._toggle_star_many(conversations)

    def _toggle_star_many(self, conversations: list[Conversation]) -> None:
        # As with read state, one aggregate command gives the whole selection a
        # deterministic state even when the conversations are mixed.
        is_starred = not any(conversation.is_starred for conversation in conversations)
        originals = {
            mail.id: mail.is_starred
            for conversation in conversations
            for mail in conversation.emails
        }
        for conversation in conversations:
            for mail in conversation.emails:
                mail.is_starred = is_starred
                self._db.set_email_starred(mail.id, is_starred)

        def revert() -> None:
            for conversation in conversations:
                for mail in conversation.emails:
                    old_starred = originals[mail.id]
                    mail.is_starred = old_starred
                    self._db.set_email_starred(mail.id, old_starred)
            self._after_flag_change(conversations)

        self._after_flag_change(conversations)
        uids = [
            uid
            for conversation in conversations
            for uid in mail_sync.server_uids(conversation)
        ]
        self._run_flag_worker(
            uids, imap_session.FLAG_FLAGGED, should_add=is_starred, revert=revert
        )

    # Clear the is_unread flag for a whole conversation: locally, in the badges
    # and list, and on the server. A no-op if it's already read, so reopening a
    # read thread costs nothing.
    def _mark_conversation_read(self, conversation: Conversation) -> None:
        if not conversation.is_unread:
            return

        for mail in conversation.emails:
            mail.is_unread = False
            self._db.mark_email_read(mail.id)

        def revert() -> None:
            for mail in conversation.emails:
                mail.is_unread = True
                self._db.mark_email_unread(mail.id)
            self._after_flag_change(conversation)

        self._after_flag_change(conversation)
        uids = mail_sync.server_uids(conversation)
        self._run_flag_worker(
            uids, imap_session.FLAG_SEEN, should_add=True, revert=revert
        )

    # Update badges and the list after a flag change, keeping this
    # conversation selected (so the reader doesn't reload).
    def _after_flag_change(
        self, conversations: Conversation | list[Conversation]
    ) -> None:
        if isinstance(conversations, Conversation):
            keep_id = conversations.id
        else:
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
        password = secrets.lookup_password(account.id)
        if not password:
            return
        change = FlagChange(
            folder_name=folder.name,
            uids=tuple(uids),
            flag=flag,
            should_add=should_add,
        )
        thread = threading.Thread(
            target=self._flag_worker,
            args=(account, password, change, revert),
            daemon=True,
        )
        thread.start()

    # Runs on the worker thread: network only, no Gtk/database access.
    def _flag_worker(
        self,
        account: Account,
        password: str,
        change: FlagChange,
        revert: Callable[[], None],
    ) -> None:
        try:
            mail_sync.set_flag(
                account,
                password,
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

    def _on_action_failed(
        self, revert: Callable[[], None], host: str, error: Exception
    ) -> bool:
        revert()
        _category, message = errors.classify(error, host)
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
        popover.insert_action_group("context", self._context_actions(conversation))
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

    def _context_actions(self, _conversation: Conversation) -> Gio.SimpleActionGroup:
        """Create actions for the current selection and its context menu row."""
        actions = Gio.SimpleActionGroup()

        toggle_read = Gio.SimpleAction.new("toggle-read", None)
        toggle_read.connect("activate", self._on_toggle_read)
        actions.add_action(toggle_read)

        toggle_star = Gio.SimpleAction.new("toggle-star", None)
        toggle_star.connect("activate", self._on_toggle_star)
        actions.add_action(toggle_star)

        archive = Gio.SimpleAction.new("archive", None)
        archive.connect("activate", self._on_archive)
        actions.add_action(archive)

        trash = Gio.SimpleAction.new("trash", None)
        trash.connect("activate", self._on_trash)
        actions.add_action(trash)

        move = Gio.SimpleAction.new("move", GLib.VariantType.new("s"))
        move.connect("activate", self._on_move)
        actions.add_action(move)

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
