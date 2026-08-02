import logging
import threading
from datetime import datetime
from gettext import gettext as _
from gettext import ngettext

from gi.repository import Adw, Gio, GLib

from . import mail_sync
from .core import compose, secrets
from .core.models.account import Account
from .core.models.folder import Folder
from .core.net import errors, imap_session
from .window_parts import MainWindowParts
from .window_types import (
    PAGE_EMPTY,
    PAGE_LOADING,
    SECONDS_PER_MINUTE,
    SETTING_SYNC_INTERVAL,
    OutboxResult,
)

logger = logging.getLogger(__name__)


class SyncMixin(MainWindowParts):
    """Syncing, draining the Outbox, and the connection banner."""

    def _drain_outbox(self) -> None:
        account = self._account
        if account is None:
            return

        outbox = next(
            (
                folder
                for folder in self._db.folders_for_account(account.id)
                if folder.name == mail_sync.OUTBOX_FOLDER
            ),
            None,
        )
        if outbox is None:
            return

        pending = self._db.emails_in_folder(outbox.id)
        if not pending:
            return

        password = secrets.lookup_password(account.id)
        if not password:
            return

        jobs = []
        for mail in pending:
            raw = self._db.get_raw_message(mail.id)
            if raw is None:
                continue
            jobs.append((mail.id, mail.subject, compose.extract_recipients(raw), raw))
        if not jobs:
            return

        thread = threading.Thread(
            target=self._outbox_worker,
            args=(account, password, jobs),
            daemon=True,
        )
        thread.start()

    # Runs on the worker thread: network only, no Gtk/database access. Failures
    # travel back as the exception itself -- the mail stays in the Outbox, so
    # the user has to be told why rather than left believing it was sent.
    def _outbox_worker(
        self,
        account: Account,
        password: str,
        jobs: list[tuple[int, str, list[str], bytes]],
    ) -> None:
        results: list[OutboxResult] = []
        for email_id, subject, recipients, raw in jobs:
            try:
                mail_sync.send_message(
                    account, password, account.email, recipients, raw
                )
            except Exception as error:
                logger.exception(
                    "could not send queued message %d (%r) to %s via %s",
                    email_id,
                    subject,
                    ", ".join(recipients),
                    account.smtp_host,
                )
                results.append(OutboxResult(email_id, subject, raw, error))
            else:
                results.append(OutboxResult(email_id, subject, raw, None))
        GLib.idle_add(self._on_outbox_drained, results)

    # Back on the main thread: safe to touch the database and widgets.
    def _on_outbox_drained(self, results: list[OutboxResult]) -> bool:
        account = self._account
        if account is None:
            return False

        sent_folder: Folder | None = None
        sent_count = 0
        for result in results:
            if result.error is not None:
                continue
            if sent_folder is None:
                sent_folder = self._db.get_or_create_folder(
                    account.id,
                    mail_sync.SENT_FOLDER,
                    mail_sync.icon_for_folder(mail_sync.SENT_FOLDER),
                )
            row = self._db.save_email(
                sent_folder.id,
                sender=account.email,
                sender_address=account.email,
                subject=result.subject,
                preview=result.subject,
                date=datetime.now().strftime("%b %d"),
                is_unread=False,
            )
            self._db.save_raw_message(row.id, result.raw)
            self._db.delete_email(result.email_id)
            sent_count += 1

        if sent_count:
            self._reload_folders()
            self._refresh_conversations()
            self._toast(_("Sent {n} queued message(s).").format(n=sent_count))

        # Queued mail that could not be sent is still in the Outbox, so say so
        # -- silently leaving it there reads as "sent" to the user.
        send_errors = [result.error for result in results if result.error is not None]
        if send_errors:
            category, message = errors.classify(send_errors[0], account.smtp_host)
            self._show_connection_banner(
                ngettext(
                    "Couldn't send a queued message. {reason}",
                    "Couldn't send {n} queued messages. {reason}",
                    len(send_errors),
                ).format(n=len(send_errors), reason=message),
                self._retry_button_label(category),
            )
        return False

    def _start_sync(
        self,
        in_background: bool = False,
        folder_name: str | None = None,
        offset: int = 0,
    ) -> None:
        # Don't pile background syncs (folder clicks, the poll timer) on top of
        # one already running.
        if in_background and self._is_syncing:
            return
        account = self._account
        password = secrets.lookup_password(account.id) if account else None
        if account is None or not password:
            if not in_background:
                self._toast(_("No saved password for this account."))
            return

        self._set_syncing(True)
        if self.conversation_stack.get_visible_child_name() == PAGE_EMPTY:
            self.conversation_stack.set_visible_child_name(PAGE_LOADING)
        thread = threading.Thread(
            target=self._sync_worker,
            args=(account, password, folder_name, offset),
            daemon=True,
        )
        thread.start()

    # Refresh on a timer using the configured interval (0 = manual only).
    def _reschedule_sync(self) -> None:
        if self._sync_timer_id:
            GLib.source_remove(self._sync_timer_id)
            self._sync_timer_id = 0
        minutes = self._settings.get_int(SETTING_SYNC_INTERVAL)
        if minutes > 0:
            self._sync_timer_id = GLib.timeout_add_seconds(
                minutes * SECONDS_PER_MINUTE, self._on_sync_tick
            )

    def _on_sync_tick(self) -> bool:
        if self._account is not None and self._is_online and not self._is_syncing:
            self._drain_outbox()
            self._start_sync(in_background=True)
        return True

    # Runs on the worker thread: network only, no Gtk/database access.
    def _sync_worker(
        self,
        account: Account,
        password: str,
        folder_name: str | None,
        offset: int = 0,
    ) -> None:
        try:
            result = mail_sync.fetch_mailbox(
                account, password, folder_name, offset=offset
            )
        except Exception as error:
            logger.exception(
                "sync failed for %s on %s (folder %s, offset %d)",
                account.email,
                account.imap_host,
                folder_name or "inbox",
                offset,
            )
            category, message = errors.classify(error, account.imap_host)
            GLib.idle_add(self._on_sync_error, category, message)
            return
        GLib.idle_add(self._on_sync_done, result)

    # Back on the main thread: safe to touch the database and widgets.
    def _on_sync_done(self, result: mail_sync.SyncResult) -> bool:
        account = self._account
        if account is None:
            return False

        # Remember the open conversation so a background poll doesn't yank it.
        selected = self._selected_conversation()
        keep_id = selected.id if selected is not None else None

        mailboxes = [
            mailbox
            for mailbox in result.folders
            if mailbox.name not in mail_sync.NAMESPACE_ROOTS
        ]

        # Shortest name first: a parent's name is a prefix of its children's, so
        # every parent is stored before a child looks it up.
        for mailbox in sorted(mailboxes, key=lambda box: len(box.name)):
            name, delimiter = mailbox.name, mailbox.delimiter
            selectable = imap_session.ATTR_NOSELECT not in mailbox.flags
            icon = mail_sync.icon_for_folder(name) if selectable else "folder-symbolic"
            folder = self._db.get_or_create_folder(account.id, name, icon)

            parent_name = mail_sync.parent_mailbox_name(name, delimiter)
            parent = self._db.get_folder_by_name(account.id, parent_name)
            self._db.set_folder_parent(
                folder.id, parent.id if parent else None, delimiter
            )

        if mailboxes:
            # Mirror the server's folder list, keeping only the local Outbox.
            # This clears stale rows like a duplicate "INBOX" from earlier
            # versions.
            names = {box.name for box in mailboxes} | {mail_sync.OUTBOX_FOLDER}
            self._db.prune_folders(account.id, names)

        target = self._db.get_or_create_folder(
            account.id, result.folder, mail_sync.icon_for_folder(result.folder)
        )
        new_messages: list[mail_sync.MessageHeader] = []
        for message in result.messages:
            if (target.id, message.uid) in self._move_tombstones:
                continue
            added = self._db.save_incoming_email(target.id, message)
            if added and message.is_unread:
                new_messages.append(message)
        if result.all_uids is not None:
            self._db.prune_stale_emails(target.id, result.all_uids)
            # Do this after filtering the fetched headers: an older snapshot
            # can still contain a UID that the authoritative set says has
            # already left the source folder.
            self._confirm_move_tombstones(target.id, result.all_uids)
        self._db.reassign_conversations(target.id)

        # From every fetched header, not just the newly added ones, so an
        # existing install fills its contacts on the next sync.
        self._db.save_contacts(
            [address for message in result.messages for address in message.addresses]
        )

        # Update paging state: track the deepest page loaded (max() so a
        # newest-page poll never forgets how far the user has scrolled back),
        # and offer "more" only while messages remain beyond it.
        reached = result.offset + len(result.messages)
        loaded = min(result.exists, max(self._loaded_counts.get(target.id, 0), reached))
        self._loaded_counts[target.id] = loaded
        self._folders_with_more_mail[target.id] = result.exists > loaded

        arrived_elsewhere = self._apply_unread_counts(account, result.unread_counts)

        self._set_syncing(False)
        self._reload_folders()
        self._refresh_conversations(keep_id=keep_id)
        self.connection_banner.set_revealed(False)

        self._notify_arrivals(new_messages, target.id, arrived_elsewhere)
        return False

    def _notify_arrivals(
        self,
        messages: list[mail_sync.MessageHeader],
        folder_id: int,
        arrived_elsewhere: dict[str, int],
    ) -> None:
        # Only nag about new mail when the user isn't already looking.
        if self.is_active():
            return
        if messages:
            self._notify_new_mail(messages, folder_id)
        if arrived_elsewhere:
            self._notify_unread_elsewhere(arrived_elsewhere)

    def _apply_unread_counts(
        self, account: Account, counts: dict[str, int]
    ) -> dict[str, int]:
        """Store the server's unread counts; return how many arrived per folder.

        A folder we have no earlier count for is only recorded -- on the first
        sync every count would otherwise read as new mail.
        """
        arrived: dict[str, int] = {}
        for name, count in counts.items():
            folder = self._db.get_folder_by_name(account.id, name)
            if folder is None:
                continue
            previous = self._remote_unread_counts.get(folder.id)
            if previous is not None and count > previous:
                arrived[mail_sync.display_name_for_folder(name)] = count - previous
            self._remote_unread_counts[folder.id] = count
        return arrived

    def _notify_unread_elsewhere(self, arrived: dict[str, int]) -> None:
        """New mail in a folder we didn't fetch, so there are no headers to
        name -- only the count and where it landed."""
        if not self._settings.get_boolean("notifications"):
            return
        app = self.get_application()
        if app is None:
            return

        total = sum(arrived.values())
        notification = Gio.Notification.new(
            ngettext("{n} new message", "{n} new messages", total).format(n=total)
        )
        notification.set_body(", ".join(arrived))
        notification.set_default_action("app.focus-mail")
        app.send_notification("new-mail-elsewhere", notification)

    def _notify_new_mail(
        self, messages: list[mail_sync.MessageHeader], folder_id: int
    ) -> None:
        if not self._settings.get_boolean("notifications"):
            return
        app = self.get_application()
        if app is None:
            return

        if len(messages) == 1:
            notification = Gio.Notification.new(messages[0].sender)
            notification.set_body(messages[0].subject)
            # Clicking it opens that thread, which is what marks it read.
            notification.set_default_action_and_target(
                "app.open-mail", GLib.Variant("(is)", (folder_id, messages[0].uid))
            )
        else:
            senders = ", ".join(dict.fromkeys(message.sender for message in messages))
            notification = Gio.Notification.new(
                _("{n} new messages").format(n=len(messages))
            )
            notification.set_body(senders)
            notification.set_default_action("app.focus-mail")

        app.send_notification("new-mail", notification)

    def _on_sync_error(self, category: str, message: str) -> bool:
        self._set_syncing(False)
        self._show_connection_banner(message, self._retry_button_label(category))
        return False

    @staticmethod
    def _retry_button_label(category: str) -> str:
        # Auth failures aren't worth a Retry button (same password); everything
        # else is a transient connection problem the user can retry.
        return "" if category == errors.CATEGORY_AUTH else _("Retry")

    def _show_connection_banner(self, title: str, button_label: str = "") -> None:
        self.connection_banner.set_title(title)
        self.connection_banner.set_button_label(button_label)
        self.connection_banner.set_revealed(True)

    def _show_offline_banner(self) -> None:
        self._show_connection_banner(
            _("You're offline. Postcard will reconnect when your connection returns.")
        )

    def _on_banner_retry(self, _banner: Adw.Banner) -> None:
        self.connection_banner.set_revealed(False)
        if self._account is not None:
            self._drain_outbox()
            self._start_sync()

    # network-changed fires on any change; act only on real online/offline flips.
    def _on_network_changed(
        self, _monitor: Gio.NetworkMonitor, is_available: bool
    ) -> None:
        if is_available == self._is_online:
            return
        self._is_online = is_available
        if not is_available:
            self._show_offline_banner()
            return
        self.connection_banner.set_revealed(False)
        if self._account is not None:
            self._drain_outbox()
            self._start_sync()

    def _notify_background(self) -> None:
        if self._has_notified_background:
            return

        self._has_notified_background = True

        app = self.get_application()
        if app is None:
            return

        notification = Gio.Notification.new(_("Postcard is running in the background"))
        notification.set_body(_("It will keep checking for new mail. Quit to stop."))
        notification.set_default_action("app.focus-mail")
        app.send_notification("running-background", notification)

    def _set_syncing(self, is_syncing: bool) -> None:
        self._is_syncing = is_syncing
        self.refresh_button.set_sensitive(not is_syncing)
        self.sync_spinner.set_visible(is_syncing)
        if is_syncing:
            self.sync_spinner.start()
        else:
            self.sync_spinner.stop()
