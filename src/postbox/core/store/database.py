import os
import sqlite3

from gi.repository import GLib

from ..models.account import Account
from ..models.email import Email
from ..models.folder import Folder


class Database:
    def __init__(self, path: str | None = None) -> None:
        if path is None:
            data_dir = os.path.join(GLib.get_user_data_dir(), "postbox")
            os.makedirs(data_dir, exist_ok=True)
            path = os.path.join(data_dir, "postbox.db")

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
                smtp_port INTEGER NOT NULL
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
                unread INTEGER NOT NULL DEFAULT 1
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_emails_uid
                ON emails (folder_id, server_id);
            """
        )
        self._conn.commit()

    # --- accounts -----------------------------------------------------------

    def _account_from_row(self, row: sqlite3.Row) -> Account:
        return Account(
            id=row["id"],
            email=row["email"],
            display_name=row["display_name"],
            imap_host=row["imap_host"],
            imap_port=row["imap_port"],
            smtp_host=row["smtp_host"],
            smtp_port=row["smtp_port"],
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
        smtp_port: int
    ) -> Account:
        cursor = self._conn.execute(
            """
            INSERT INTO accounts (email, display_name, imap_host, imap_port, smtp_host, smtp_port)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (email, display_name, imap_host, imap_port, smtp_host, smtp_port),
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

    # --- emails -----------------------------------------------------------

    def emails_in_folder(self, folder_id: int) -> list[Email]:
        rows = self._conn.execute(
            "SELECT * FROM emails WHERE folder_id = ? ORDER BY id DESC", (folder_id,)
        ).fetchall()
        return [self._email_from_row(row) for row in rows]

    def unread_count_in_folder(self, folder_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM emails WHERE folder_id = ? AND unread = 1",
            (folder_id,),
        ).fetchone()
        return row["n"]

    def mark_email_read(self, email_id: int) -> None:
        self._conn.execute(
            "UPDATE emails SET unread = 0 WHERE id = ?", (email_id,)
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
            INSERT INTO emails (folder_id, server_id, sender, subject, preview, date, unread)
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
    ) -> bool:
        """Insert one fetched email; skip it if we already have it."""
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO emails
                (folder_id, server_id, sender, subject, preview, date, unread)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (folder_id, server_id, sender, subject, preview, date, int(unread)),
        )
        self._conn.commit()
        return cursor.rowcount > 0

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
        )
