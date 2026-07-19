<div align="center">
  <img src="data/icons/hicolor/128x128/apps/in.gxanshu.postbox.png" width="96" alt="Postbox icon">

  # Postbox

  A modern email client for GNOME.
</div>

Postbox started out as Geary's three-pane layout — folders, conversations, reading pane —
rebuilt on a modern stack: GTK 4, libadwaita, and Python. It's quickly growing into its own
alternative: modern technology, a simple codebase, and a modern UI, without the years of
accumulated complexity. It's built and shipped as a Flatpak.

<div align="center">
  <img src="data/screenshots/main-window.png" alt="Postbox showing the three-pane layout with folders, conversations, and reader">
</div>

## Features

- Multiple IMAP/SMTP accounts, with passwords stored in the system keyring
- Conversations grouped into threads
- Instant full-text search across your mail
- Offline reading from a local cache
- HTML and plain-text mail, with remote images blocked until you allow them
- Compose, reply, and forward, with Cc/Bcc, a signature, and a Drafts/Outbox that never
  loses a message
- Archive, trash, move, and undo, with desktop notifications for new mail

## Building & running

Postbox is built and run entirely as a Flatpak — the same way it ships to users. There is
no host-level `python app.py`; everything goes through [`just`](https://github.com/casey/just):

```bash
just init      # one-time: add Flathub, install the GNOME runtime + SDK
just build     # build the Flatpak from the working tree, install for --user
just run       # build, then launch (the normal dev loop)
```

Requires `flatpak` and `flatpak-builder` on the host — Python, GTK, and everything else
comes from the GNOME SDK.

## Tech stack

GTK 4 · libadwaita · Blueprint (`.blp`) UI · WebKitGTK for HTML mail · SQLite (with FTS5
for search) · Python's stdlib `imaplib`/`smtplib` for networking · libsecret for credentials.

## License

[GPL-3.0-or-later](COPYING).
