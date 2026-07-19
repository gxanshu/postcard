import os
import sqlite3

from gi.repository import GLib

from .. import threader
from ..models.account import Account
from ..models.conversation import Conversation
from ..models.email import Email
from ..models.folder import Folder


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query: each word matched as a prefix."""
    terms = [f'"{word.replace(chr(34), chr(34) * 2)}"*' for word in text.split()]
    return " ".join(terms)


def _arrival_key(mail: Email) -> int:
    """A proxy for when a message arrived, for ordering threads/messages.

    The IMAP UID (server_id) is guaranteed by the protocol to increase with
    arrival order within a folder, unlike our local autoincrement id: once
    load-on-scroll backfills older mail in a later fetch, that older mail gets
    a *newer* local id, so sorting by id would put it first instead of last.
    A message with no UID yet (a Sent copy saved right after sending, before
    the next sync confirms it) sorts as the newest.
    """
    try:
        return int(mail.server_id)
    except (TypeError, ValueError):
        return 2**31 - 1


class Database:
    def __init__(self, path: str | None = None) -> None:
        if path is None:
            data_dir = os.path.join(GLib.get_user_data_dir(), "postcard")
            os.makedirs(data_dir, exist_ok=True)
            path = os.path.join(data_dir, "postcard.db")

        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

        self._create_tables()

    def close(self) -> None:
        self._conn.close()

    # --- schema -----------------------------------------------------------

    def _create_tables(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL,
                display_name TEXT NOT NULL,
                imap_host TEXT NOT NULL,
                imap_port INTEGER NOT NULL,
                smtp_host TEXT NOT NULL,
                smtp_port INTEGER NOT NULL,
                imap_security TEXT NOT NULL DEFAULT 'tls',
                smtp_security TEXT NOT NULL DEFAULT 'starttls'
            );

            CREATE TABLE IF NOT EXISTS folders (
                id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                name TEXT NOT NULL,
                icon_name TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS emails (
                id INTEGER PRIMARY KEY,
                folder_id INTEGER NOT NULL REFERENCES folders(id),
                server_id TEXT,
                sender TEXT NOT NULL,
                subject TEXT NOT NULL,
                preview TEXT NOT NULL,
                date TEXT NOT NULL,
                unread INTEGER NOT NULL DEFAULT 1,
                starred INTEGER NOT NULL DEFAULT 0,
                raw_message BLOB,
                message_id TEXT,
                in_reply_to TEXT,
                reference_ids TEXT,
                conversation_id INTEGER
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_emails_uid
                ON emails (folder_id, server_id);

            -- Full-text search index over the searchable columns. It mirrors
            -- the emails table (content='emails'), so triggers keep it in sync.
            CREATE VIRTUAL TABLE IF NOT EXISTS emails_fts USING fts5(
                sender, subject, preview,
                content='emails', content_rowid='id'
            );

            CREATE TRIGGER IF NOT EXISTS emails_fts_insert AFTER INSERT ON emails BEGIN
                INSERT INTO emails_fts(rowid, sender, subject, preview)
                VALUES (new.id, new.sender, new.subject, new.preview);
            END;

            CREATE TRIGGER IF NOT EXISTS emails_fts_delete AFTER DELETE ON emails BEGIN
                INSERT INTO emails_fts(emails_fts, rowid, sender, subject, preview)
                VALUES ('delete', old.id, old.sender, old.subject, old.preview);
            END;

            -- Re-index any rows that predate the FTS table (cheap for a
            -- personal mailbox, and saves deleting the database by hand).
            INSERT INTO emails_fts(emails_fts) VALUES ('rebuild');
            """
        )

        self._conn.commit()

    # --- accounts -----------------------------------------------------------

    def _account_from_row(self, row: sqlite3.Row) -> Account:
        imap_sec = row["imap_security"]
        if imap_sec is None:
            imap_sec = "tls"  # legacy: always used IMAP4_SSL
        smtp_sec = row["smtp_security"]
        if smtp_sec is None:
            smtp_sec = "tls" if row["smtp_port"] == 465 else "starttls"
        return Account(
            id=row["id"],
            email=row["email"],
            display_name=row["display_name"],
            imap_host=row["imap_host"],
            imap_port=row["imap_port"],
            smtp_host=row["smtp_host"],
            smtp_port=row["smtp_port"],
            imap_security=imap_sec,
            smtp_security=smtp_sec,
        )

    def accounts(self) -> list[Account]:
        rows = self._conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        return [self._account_from_row(row) for row in rows]

    def save_account(
        self,
        email: str,
        display_name: str,
        imap_host: str,
        imap_port: int,
        smtp_host: str,
        smtp_port: int,
        imap_security: str = "tls",
        smtp_security: str | None = None,
    ) -> Account:
        if smtp_security is None:
            smtp_security = "tls" if smtp_port == 465 else "starttls"
        cursor = self._conn.execute(
            """
            INSERT INTO accounts
                (email, display_name, imap_host, imap_port, smtp_host, smtp_port,
                 imap_security, smtp_security)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                email,
                display_name,
                imap_host,
                imap_port,
                smtp_host,
                smtp_port,
                imap_security,
                smtp_security,
            ),
        )
        self._conn.commit()

        row = self._conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return self._account_from_row(row)

    def delete_account(self, account_id: int) -> None:
        self._conn.execute(
            """
            DELETE FROM emails WHERE folder_id IN (
                SELECT id FROM folders WHERE account_id = ?
            )
            """,
            (account_id,),
        )
        self._conn.execute("DELETE FROM folders WHERE account_id = ?", (account_id,))
        self._conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        self._conn.commit()

    # --- folders -----------------------------------------------------------

    def folders_for_account(self, account_id: int) -> list[Folder]:
        rows = self._conn.execute(
            "SELECT * FROM folders WHERE account_id = ? ORDER BY id", (account_id,)
        ).fetchall()
        return [
            Folder(
                id=row["id"],
                account_id=row["account_id"],
                name=row["name"],
                icon_name=row["icon_name"],
            )
            for row in rows
        ]

    def get_or_create_folder(
        self, account_id: int, name: str, icon_name: str
    ) -> Folder:
        row = self._conn.execute(
            "SELECT * FROM folders WHERE account_id = ? AND name = ?",
            (account_id, name),
        ).fetchone()
        if row is not None:
            return Folder(
                id=row["id"],
                account_id=row["account_id"],
                name=row["name"],
                icon_name=row["icon_name"],
            )

        cursor = self._conn.execute(
            "INSERT INTO folders (account_id, name, icon_name) VALUES (?, ?, ?)",
            (account_id, name, icon_name),
        )
        self._conn.commit()
        assert cursor.lastrowid is not None
        return Folder(
            id=cursor.lastrowid,
            account_id=account_id,
            name=name,
            icon_name=icon_name,
        )

    def prune_folders(self, account_id: int, keep_names: set[str]) -> None:
        """Delete an account's folders (and their emails) whose names aren't in
        keep_names. Used to mirror the server's folder list and clear stale rows
        such as a duplicate "INBOX" left by an earlier version."""
        rows = self._conn.execute(
            "SELECT id, name FROM folders WHERE account_id = ?", (account_id,)
        ).fetchall()
        for row in rows:
            if row["name"] in keep_names:
                continue
            self._conn.execute("DELETE FROM emails WHERE folder_id = ?", (row["id"],))
            self._conn.execute("DELETE FROM folders WHERE id = ?", (row["id"],))
        self._conn.commit()

    # --- emails -----------------------------------------------------------

    def emails_in_folder(self, folder_id: int) -> list[Email]:
        rows = self._conn.execute(
            "SELECT * FROM emails WHERE folder_id = ? ORDER BY id DESC", (folder_id,)
        ).fetchall()
        return [self._email_from_row(row) for row in rows]

    def reassign_conversations(self, folder_id: int) -> None:
        """Recompute the thread grouping for a folder and store it on each row."""
        emails = self.emails_in_folder(folder_id)
        for email_id, conversation_id in threader.group(emails).items():
            self._conn.execute(
                "UPDATE emails SET conversation_id = ? WHERE id = ?",
                (conversation_id, email_id),
            )
        self._conn.commit()

    def conversations_in_folder(self, folder_id: int) -> list[Conversation]:
        """Group a folder's emails into threads, newest thread first."""
        rows = self._conn.execute(
            "SELECT * FROM emails WHERE folder_id = ?", (folder_id,)
        ).fetchall()

        groups: dict[int, list[Email]] = {}
        for row in rows:
            email = self._email_from_row(row)
            key = email.conversation_id or email.id
            groups.setdefault(key, []).append(email)

        conversations = []
        for mails in groups.values():
            mails.sort(key=_arrival_key)  # oldest first, so .latest is right
            conversations.append(Conversation(mails))
        conversations.sort(key=lambda c: _arrival_key(c.latest), reverse=True)
        return conversations

    def search_conversations(self, folder_id: int, query: str) -> list[Conversation]:
        """Full-text search a folder; return the matching conversations."""
        match = _fts_query(query)
        if not match:
            return self.conversations_in_folder(folder_id)

        rows = self._conn.execute(
            """
            SELECT e.id, e.conversation_id
            FROM emails_fts f
            JOIN emails e ON e.id = f.rowid
            WHERE e.folder_id = ? AND emails_fts MATCH ?
            """,
            (folder_id, match),
        ).fetchall()

        keys = {row["conversation_id"] or row["id"] for row in rows}
        return [c for c in self.conversations_in_folder(folder_id) if c.id in keys]

    def unread_count_in_folder(self, folder_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM emails WHERE folder_id = ? AND unread = 1",
            (folder_id,),
        ).fetchone()
        return row["n"]

    def mark_email_read(self, email_id: int) -> None:
        self._conn.execute("UPDATE emails SET unread = 0 WHERE id = ?", (email_id,))
        self._conn.commit()

    def mark_email_unread(self, email_id: int) -> None:
        self._conn.execute("UPDATE emails SET unread = 1 WHERE id = ?", (email_id,))
        self._conn.commit()

    def set_email_starred(self, email_id: int, starred: bool) -> None:
        self._conn.execute(
            "UPDATE emails SET starred = ? WHERE id = ?", (int(starred), email_id)
        )
        self._conn.commit()

    def move_email(self, email_id: int, folder_id: int) -> None:
        self._conn.execute(
            "UPDATE emails SET folder_id = ? WHERE id = ?", (folder_id, email_id)
        )
        self._conn.commit()

    def get_raw_message(self, email_id: int) -> bytes | None:
        row = self._conn.execute(
            "SELECT raw_message FROM emails WHERE id = ?", (email_id,)
        ).fetchone()

        return row["raw_message"] if row else None

    def save_raw_message(self, email_id: int, raw: bytes) -> None:
        self._conn.execute(
            "UPDATE emails SET raw_message = ? where id = ?", (raw, email_id)
        )
        self._conn.commit()

    def save_email(
        self,
        folder_id: int,
        sender: str,
        subject: str,
        preview: str,
        date: str,
        unread: bool,
        server_id: str | None = None,
    ) -> Email:
        cursor = self._conn.execute(
            """
            INSERT INTO emails
                (folder_id, server_id, sender, subject, preview, date, unread)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (folder_id, server_id, sender, subject, preview, date, int(unread)),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM emails WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return self._email_from_row(row)

    def save_incoming_email(
        self,
        folder_id: int,
        server_id: str,
        sender: str,
        subject: str,
        preview: str,
        date: str,
        unread: bool,
        starred: bool = False,
        message_id: str = "",
        in_reply_to: str = "",
        references: str = "",
    ) -> bool:
        """Insert one fetched email; skip it if we already have it."""
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO emails
                (folder_id, server_id, sender, subject, preview, date, unread,
                 starred, message_id, in_reply_to, reference_ids)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                folder_id,
                server_id,
                sender,
                subject,
                preview,
                date,
                int(unread),
                int(starred),
                message_id,
                in_reply_to,
                references,
            ),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def delete_email(self, email_id: int) -> None:
        self._conn.execute("DELETE FROM emails WHERE id = ?", (email_id,))
        self._conn.commit()

    def _email_from_row(self, row: sqlite3.Row) -> Email:
        return Email(
            id=row["id"],
            folder_id=row["folder_id"],
            server_id=row["server_id"],
            sender=row["sender"],
            subject=row["subject"],
            preview=row["preview"],
            date=row["date"],
            unread=bool(row["unread"]),
            starred=bool(row["starred"]),
            message_id=row["message_id"] or "",
            in_reply_to=row["in_reply_to"] or "",
            references=row["reference_ids"] or "",
            conversation_id=row["conversation_id"],
        )
