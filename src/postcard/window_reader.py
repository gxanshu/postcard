import logging
import shutil
import threading
from gettext import gettext as _
from pathlib import Path

from gi.repository import Gio, GLib, Gtk

from . import mail_sync
from .core import secrets
from .core.models.account import Account
from .core.models.attachment import Attachment
from .core.models.conversation import Conversation
from .core.models.email import Email
from .core.net import errors
from .message_view import LoadCallback, MessageView
from .window_parts import MainWindowParts
from .window_types import PAGE_EMPTY, BodyRequest

logger = logging.getLogger(__name__)


class ReaderMixin(MainWindowParts):
    """The reading pane: thread, bodies, and attachments."""

    def _update_reader(self) -> None:
        # The conversation store only exists once a mail view is loaded, and
        # there is nothing to read before then.
        if self._account is None:
            return
        selected = self._selected_conversations()
        if len(selected) != 1:
            self._rendered_id = None
            self._active_view = None
            self._set_reply_forward_enabled(False)
            self._set_mail_actions_enabled(bool(selected))
            if selected:
                self._update_action_buttons(selected)
            # Hiding the pane isn't enough: the views behind it keep their
            # WebViews, and each one holds a web process open.
            self._clear_thread()
            self.reader_stack.set_visible_child_name(PAGE_EMPTY)
            return

        conversation = selected[0]
        self._update_action_buttons(selected)
        self._set_reply_forward_enabled(False)

        # Already showing this thread (e.g. after a flag change) — don't rebuild.
        if conversation.id == self._rendered_id:
            view = self._active_view
            self._set_reply_forward_enabled(view is not None and view.raw is not None)
            self.reader_stack.set_visible_child_name("message")
            return

        self._rendered_id = conversation.id
        self._active_view = None
        self._render_thread(conversation)
        # Opening a conversation marks it read (like most mail clients).
        self._mark_conversation_read(conversation)

    # Reflect the selected conversation's state on the action buttons.
    def _update_action_buttons(
        self, conversations: Conversation | list[Conversation]
    ) -> None:
        if isinstance(conversations, Conversation):
            selected = [conversations]
        else:
            selected = conversations
        self._set_mail_actions_enabled(True)

        if any(conversation.is_unread for conversation in selected):
            self.mark_read_button.set_icon_name("mail-read-symbolic")
            self.mark_read_button.set_tooltip_text(_("Mark Read"))
        else:
            self.mark_read_button.set_icon_name("mail-unread-symbolic")
            self.mark_read_button.set_tooltip_text(_("Mark Unread"))

        if any(conversation.is_starred for conversation in selected):
            self.star_button.set_icon_name("starred-symbolic")
            self.star_button.set_tooltip_text(_("Unstar"))
        else:
            self.star_button.set_icon_name("non-starred-symbolic")
            self.star_button.set_tooltip_text(_("Star"))

    # Empty the reading pane, releasing each view's WebView as it goes.
    def _clear_thread(self) -> None:
        child = self.thread_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.thread_box.remove(child)
            if isinstance(child, MessageView):
                child.release()
            child = next_child

    # Build one MessageView per email, newest first. The newest starts expanded
    # (which loads its body); older ones load lazily when the user expands them.
    def _render_thread(self, conversation: Conversation) -> None:
        self.reader_subject.set_label(conversation.subject)
        self._clear_thread()

        should_load_remote_images = self._settings.get_boolean("load-remote-images")
        emails = list(reversed(conversation.emails))
        for index, mail in enumerate(emails):
            is_newest = index == 0
            view = MessageView(
                mail,
                on_load=self._load_body,
                on_save_attachment=self._save_attachment,
                on_open_attachment=self._open_attachment,
                on_rendered=self._on_newest_rendered if is_newest else None,
                is_expanded=is_newest,
                should_load_remote_images=should_load_remote_images,
                avatars=self._avatars,
            )
            self.thread_box.append(view)

        self.reader_stack.set_visible_child_name("message")

    def _on_newest_rendered(self, view: MessageView) -> None:
        if len(self._selected_conversations()) != 1:
            return
        self._active_view = view
        self._set_reply_forward_enabled(True)

    # Fetch one message's raw bytes for a MessageView: serve the cached copy if
    # we have it, else pull it over IMAP on a worker thread. (Marking read is
    # handled once per conversation in _mark_conversation_read.)
    def _load_body(self, mail: Email, callback: LoadCallback) -> None:
        cached = self._db.get_raw_message(mail.id)
        if cached is not None:
            callback(cached, None)
            return

        if not mail.server_id:
            # No UID and no cached copy: nothing to fetch until the next sync.
            callback(None, _("This message hasn't finished syncing yet."))
            return

        account = self._account
        folder = self._current_folder
        if account is None or folder is None:
            callback(None, _("No account is open."))
            return

        password = secrets.lookup_password(account.id)
        if not password:
            callback(None, _("No saved password for this account."))
            return

        request = BodyRequest(
            email_id=mail.id, uid=mail.server_id, folder_name=folder.name
        )
        thread = threading.Thread(
            target=self._body_worker,
            args=(account, password, request, callback),
            daemon=True,
        )
        thread.start()

    # Runs on the worker thread: network only, no Gtk/database access. Takes a
    # snapshot rather than the Email, which the main thread may mutate -- the
    # account and password are resolved by _load_body for the same reason.
    def _body_worker(
        self,
        account: Account,
        password: str,
        request: BodyRequest,
        callback: LoadCallback,
    ) -> None:
        try:
            raw = mail_sync.fetch_full_message(
                account, password, request.folder_name, request.uid
            )
        except Exception as error:
            logger.exception(
                "could not fetch message uid %s from %s (account %s)",
                request.uid,
                request.folder_name,
                account.email,
            )
            _category, message = errors.classify(error, account.imap_host)
            GLib.idle_add(self._deliver_body, callback, request.email_id, None, message)
            return
        GLib.idle_add(self._deliver_body, callback, request.email_id, raw, None)

    # Back on the main thread: cache the body, then hand it to the MessageView.
    def _deliver_body(
        self,
        callback: LoadCallback,
        email_id: int,
        raw: bytes | None,
        error: str | None,
    ) -> bool:
        if raw is not None:
            self._db.save_raw_message(email_id, raw)
        callback(raw, error)
        return False

    def _save_attachment(self, attachment: Attachment) -> None:
        dialog = Gtk.FileDialog(initial_name=attachment.filename)
        dialog.save(self, None, self._on_save_dialog_done, attachment)

    def _on_save_dialog_done(
        self,
        dialog: Gtk.FileDialog,
        result: Gio.AsyncResult,
        attachment: Attachment,
    ) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return  # user cancelled the dialog

        # A full disk or a read-only target fails here, not in save_finish. Left
        # unhandled the exception is swallowed by PyGObject and the user sees
        # neither the "Saved" toast nor any reason why.
        try:
            file.replace_contents(
                attachment.content, None, False, Gio.FileCreateFlags.NONE, None
            )
        except GLib.Error as error:
            logger.exception("could not save attachment to %s", file.get_path())
            self._toast(
                _("Couldn't save {name}: {msg}").format(
                    name=attachment.filename, msg=error.message
                )
            )
            return

        self._toast(_("Saved {name}.").format(name=attachment.filename))

    # Not /tmp: Flatpak gives the instance a private one, and the document
    # portal can't hand a file from there to another app -- the launch fails.
    def _open_attachment(self, attachment: Attachment) -> None:
        # Server-supplied name; "../../.bashrc" would otherwise escape the dir.
        name = Path(attachment.filename).name or "attachment"
        directory = Path(GLib.get_user_cache_dir()) / "attachments"
        shutil.rmtree(directory, ignore_errors=True)  # keep only the newest copy
        path = directory / name
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path.write_bytes(attachment.content)
        except OSError:
            logger.exception("could not write attachment %s to %s", name, path)
            self._toast(_("Couldn't open {name}.").format(name=attachment.filename))
            return

        launcher = Gtk.FileLauncher(file=Gio.File.new_for_path(str(path)))
        launcher.launch(self, None, self._on_launch_done, attachment.filename)

    def _on_launch_done(
        self,
        launcher: Gtk.FileLauncher,
        result: Gio.AsyncResult,
        filename: str,
    ) -> None:
        try:
            launcher.launch_finish(result)
        except GLib.Error as error:
            if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                return
            logger.warning("could not open attachment %s: %s", filename, error.message)
            self._toast(_("Couldn't open {name}.").format(name=filename))
