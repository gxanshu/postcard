# database.py
#
# Local SQLite cache: the offline foundation. Plain SQL, no ORM. Everything
# the UI shows is read from here — nothing is held only in memory.
#
# SPDX-License-Identifier: GPL-3.0-or-later

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
        self._seed_if_empty()

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
            """
        )
        self._conn.commit()

    # --- accounts -----------------------------------------------------------

    def accounts(self) -> list[Account]:
        rows = self._conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        return [
            Account(
                id=row["id"],
                email=row["email"],
                display_name=row["display_name"],
                imap_host=row["imap_host"],
                imap_port=row["imap_port"],
                smtp_host=row["smtp_host"],
                smtp_port=row["smtp_port"],
            )
            for row in rows
        ]

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

    # --- first-run sample data ----------------------------------------------

    # Temporary: gives the UI something real to read before Phase 4 (accounts)
    # and Phase 5 (IMAP) exist. Delete once accounts can be added by hand.
    def _seed_if_empty(self) -> None:
        if self._conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]:
            return

        account_id = self._conn.execute(
            """
            INSERT INTO accounts (email, display_name, imap_host, imap_port, smtp_host, smtp_port)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("anshu@postbox.dev", "Anshu", "imap.postbox.dev", 993, "smtp.postbox.dev", 587),
        ).lastrowid

        folder_specs = [
            ("inbox", "mail-inbox-symbolic"),
            ("Starred", "starred-symbolic"),
            ("Sent", "mail-send-symbolic"),
            ("Drafts", "document-edit-symbolic"),
            ("Trash", "user-trash-symbolic"),
        ]
        folder_ids = {}
        for name, icon_name in folder_specs:
            folder_ids[name] = self._conn.execute(
                "INSERT INTO folders (account_id, name, icon_name) VALUES (?, ?, ?)",
                (account_id, name, icon_name),
            ).lastrowid

        inbox_emails = [
            (
                "GNOME Foundation",
                "Welcome to GNOME 48",
                "Thanks for joining the community — here's what shipped this cycle and how to get involved.",
                "09:42",
                True,
            ),
            (
                "Migadu Support",
                "Your mailbox is ready",
                "Your new mailbox anshu@postbox.dev is provisioned. IMAP and SMTP settings are below.",
                "08:15",
                True,
            ),
            (
                "Ada Lovelace",
                "Re: Lunch on Thursday?",
                "Thursday works great for me. Let's meet at the usual place around noon.",
                "Yesterday",
                False,
            ),
            (
                "Vala Weekly",
                "Async/await, explained simply",
                "This week: a gentle walk through yield, plus a reader question about GListModel.",
                "Mon",
                False,
            ),
            (
                "Grace Hopper",
                "Debugging tips",
                "Attached are the notes from the talk. The bit about reading the reference, not copying it, applies here too.",
                "Sun",
                False,
            ),
        ]
        for sender, subject, preview, date, unread in inbox_emails:
            self._conn.execute(
                """
                INSERT INTO emails (folder_id, sender, subject, preview, date, unread)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (folder_ids["inbox"], sender, subject, preview, date, int(unread)),
            )

        self._conn.execute(
            """
            INSERT INTO emails (folder_id, sender, subject, preview, date, unread)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                folder_ids["Starred"],
                "Ada Lovelace",
                "Re: Lunch on Thursday?",
                "Thursday works great for me. Let's meet at the usual place around noon.",
                "Yesterday",
                0,
            ),
        )

        self._conn.execute(
            """
            INSERT INTO emails (folder_id, sender, subject, preview, date, unread)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                folder_ids["Sent"],
                "Me",
                "Re: Your mailbox is ready",
                "Thanks! Got it working. Now building an email client to actually read it in.",
                "08:31",
                0,
            ),
        )

        self._conn.execute(
            """
            INSERT INTO emails (folder_id, sender, subject, preview, date, unread)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (folder_ids["Drafts"], "Me", "(no subject)", "Hey, just wanted to say —", "10:02", 0),
        )

        self._conn.commit()
