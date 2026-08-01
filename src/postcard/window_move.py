import logging
import threading
from gettext import gettext as _
from gettext import ngettext

from gi.repository import Adw, Gio, GLib

from . import mail_sync
from .core import secrets
from .core.models.account import Account
from .core.models.conversation import Conversation
from .core.models.folder import Folder
from .core.net import errors
from .window_parts import MainWindowParts
from .window_types import MOVE_UNDO_MS, PendingMove

logger = logging.getLogger(__name__)


class MoveMixin(MainWindowParts):
    """Archive, trash and move, with an undo window."""

    def _on_archive(self, _action: Gio.SimpleAction, _param: object) -> None:
        self._start_move_by_role(mail_sync.FolderRole.ARCHIVE)

    def _on_trash(self, _action: Gio.SimpleAction, _param: object) -> None:
        self._start_move_by_role(mail_sync.FolderRole.TRASH)

    def _on_move(self, _action: Gio.SimpleAction, param: GLib.Variant) -> None:
        conversations = self._selected_conversations()
        if not conversations:
            return
        self._move_to(conversations[0], param)

    def _move_to(self, conversation: Conversation, param: GLib.Variant) -> None:
        dest = self._find_folder_by_name(param.get_string())
        if dest is not None:
            conversations = self._selected_conversations() or [conversation]
            count = len(conversations)
            title = ngettext(
                "Moved to {name}",
                "Moved {n} conversations to {name}",
                count,
            ).format(n=count, name=dest.name)
            self._start_move(conversations, dest, title)

    def _start_move_by_role(
        self, role: mail_sync.FolderRole, conversation: Conversation | None = None
    ) -> None:
        conversations = self._selected_conversations()
        if not conversations and conversation is not None:
            conversations = [conversation]
        if not conversations:
            return
        dest = self._folder_with_role(role)
        if dest is None:
            self._toast(_("No {role} folder found.").format(role=role))
            return
        count = len(conversations)
        if role == mail_sync.FolderRole.ARCHIVE:
            title = ngettext("Archived", "Archived {n} conversations", count).format(
                n=count
            )
        else:
            title = ngettext("Deleted", "Deleted {n} conversations", count).format(
                n=count
            )
        self._start_move(conversations, dest, title)

    def _folder_with_role(self, role: mail_sync.FolderRole) -> Folder | None:
        if self._account is None:
            return None
        current_id = self._current_folder.id if self._current_folder else None
        matches = [
            folder
            for folder in self._db.folders_for_account(self._account.id)
            if folder.id != current_id
            and mail_sync.role_for_folder(folder.name) == role
        ]
        if role == mail_sync.FolderRole.ARCHIVE:
            # role_for_folder maps both "Archive" and Gmail's "All Mail" to
            # ARCHIVE. When an account has both, a folder actually named
            # Archive is the one the user means -- All Mail is a view of
            # everything, so moving there wouldn't remove it from the inbox.
            for folder in matches:
                if mail_sync.FolderRole.ARCHIVE in folder.name.lower():
                    return folder
        return matches[0] if matches else None

    def _find_folder_by_name(self, name: str) -> Folder | None:
        if self._account is None:
            return None
        for folder in self._db.folders_for_account(self._account.id):
            if folder.name == name:
                return folder
        return None

    # Move a conversation optimistically: update the DB and drop it from the
    # list now, then run the real IMAP MOVE ~5s later. An Undo toast cancels the
    # server move if clicked before then.
    def _start_move(
        self, conversations: list[Conversation], dest: Folder, verb: str
    ) -> None:
        source = self._current_folder
        if source is None or dest.id == source.id:
            return

        self._commit_pending_move()

        # Pair each mail with its UID in one pass so the "has a UID" narrowing
        # survives into email_ids/uids/originals, which stay index-aligned.
        # A locally saved copy has no UID yet -- see mail_sync.server_uids.
        mails_with_uids = [
            (mail, mail.server_id)
            for conversation in conversations
            for mail in conversation.emails
            if mail.server_id is not None
        ]
        email_ids = [mail.id for mail, _ in mails_with_uids]
        uids = [uid for _, uid in mails_with_uids]
        originals = [(mail.id, source.id, uid) for mail, uid in mails_with_uids]
        tombstones = [(source.id, uid) for uid in uids]
        self._db.move_emails(email_ids, dest.id)
        for tombstone in tombstones:
            state = self._move_tombstones.setdefault(
                tombstone, {"active": 0, "awaiting": 0}
            )
            state["active"] += 1

        self._reload_folders()
        self._refresh_conversations()

        toast = Adw.Toast(title=verb, button_label=_("Undo"))
        toast.connect("button-clicked", self._on_undo_move)
        self._pending_toast = toast
        self._pending_move = PendingMove(
            email_ids=email_ids,
            uids=uids,
            originals=originals,
            source=source,
            dest=dest,
            tombstones=tombstones,
            timeout_id=GLib.timeout_add(MOVE_UNDO_MS, self._on_move_timeout),
        )
        self.toast_overlay.add_toast(toast)

    def _on_undo_move(self, _toast: Adw.Toast) -> None:
        pending = self._pending_move
        if pending is None:
            return
        GLib.source_remove(pending.timeout_id)
        self._pending_move = None
        self._pending_toast = None
        self._restore_move(pending)

    # The undo window elapsed — actually send the move to the server.
    def _on_move_timeout(self) -> bool:
        pending = self._pending_move
        self._pending_move = None
        self._pending_toast = None
        if pending is not None:
            self._run_move_worker(pending)
        return False

    # A newer action arrived: send the previous pending move now instead of
    # waiting for its timer.
    def _commit_pending_move(self) -> None:
        pending = self._pending_move
        if pending is None:
            return
        GLib.source_remove(pending.timeout_id)
        self._pending_move = None
        if self._pending_toast is not None:
            self._pending_toast.dismiss()
            self._pending_toast = None
        self._run_move_worker(pending)

    def _restore_move(self, pending: PendingMove) -> None:
        self._db.restore_emails(pending.originals)
        self._clear_move_tombstones(pending)
        self._reload_folders()
        self._refresh_conversations()

    def _clear_move_tombstones(self, pending: PendingMove, start: int = 0) -> None:
        for tombstone in pending.tombstones[start:]:
            state = self._move_tombstones.get(tombstone)
            if state is None:
                continue
            state["active"] -= 1
            if state["active"] <= 0 and state["awaiting"] <= 0:
                self._move_tombstones.pop(tombstone, None)

    def _await_move_tombstones(self, pending: PendingMove, completed: int) -> None:
        for tombstone in pending.tombstones[:completed]:
            state = self._move_tombstones.get(tombstone)
            if state is None:
                continue
            state["active"] -= 1
            state["awaiting"] += 1

    def _confirm_move_tombstones(self, folder_id: int, all_uids: set[str]) -> None:
        for (tombstone_folder_id, uid), state in list(self._move_tombstones.items()):
            if tombstone_folder_id != folder_id or uid in all_uids:
                continue
            state["awaiting"] = 0
            if state["active"] <= 0:
                self._move_tombstones.pop((tombstone_folder_id, uid), None)

    def _run_move_worker(self, pending: PendingMove) -> None:
        account = self._account
        password = secrets.lookup_password(account.id) if account else None
        if account is None or not password:
            self._restore_move(pending)
            self._toast(_("No saved password for this account."))
            return
        thread = threading.Thread(
            target=self._move_worker,
            args=(account, password, pending),
            daemon=True,
        )
        thread.start()

    # Runs on the worker thread: network only, no Gtk/database access.
    def _move_worker(
        self, account: Account, password: str, pending: PendingMove
    ) -> None:
        try:
            result = mail_sync.move_messages(
                account,
                password,
                pending.source.name,
                pending.uids,
                pending.dest.name,
            )
            GLib.idle_add(self._on_move_result, pending, result)
        except Exception as error:
            logger.exception(
                "could not move %d message(s) from %s to %s (account %s)",
                len(pending.uids),
                pending.source.name,
                pending.dest.name,
                account.email,
            )
            GLib.idle_add(self._on_move_failed, pending, account.imap_host, error)

    def _on_move_result(
        self, pending: PendingMove, result: mail_sync.MoveResult
    ) -> bool:
        completed = len(result.destination_uids)
        self._await_move_tombstones(pending, completed)
        completed_moves = list(
            zip(
                pending.email_ids[:completed],
                [pending.dest.id] * completed,
                result.destination_uids,
                strict=True,
            )
        )
        if completed_moves:
            self._db.reconcile_moved_emails(completed_moves)
        failed_index = result.failed_index
        if failed_index is None and completed < len(pending.uids):
            failed_index = completed
        if failed_index is not None:
            self._db.restore_emails(pending.originals[failed_index:])
            self._clear_move_tombstones(pending, failed_index)
        self._reload_folders()
        self._refresh_conversations()
        if result.error is not None:
            logger.warning(
                "move stopped after %d of %d message(s) from %s to %s: %s",
                completed,
                len(pending.uids),
                pending.source.name,
                pending.dest.name,
                result.error,
            )
            self._toast(_("Move failed: {msg}").format(msg=result.error))
        return False

    def _on_move_failed(
        self, pending: PendingMove, host: str, error: Exception
    ) -> bool:
        self._restore_move(pending)
        _category, message = errors.classify(error, host)
        self._toast(_("Move failed: {msg}").format(msg=message))
        return False

    # A menu of every folder except the current one, each targeting the given
    # action prefix (the toolbar uses win.move and context menus use context.move).
    def _build_move_menu(self, action_prefix: str = "win") -> Gio.Menu:
        menu = Gio.Menu()
        if self._account is None:
            return menu
        current_id = self._current_folder.id if self._current_folder else None
        for folder in self._db.folders_for_account(self._account.id):
            if folder.id == current_id:
                continue
            label = mail_sync.display_name_for_folder(
                folder.name, folder.display_delimiter
            )
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value(
                f"{action_prefix}.move", GLib.Variant.new_string(folder.name)
            )
            menu.append_item(item)
        return menu
