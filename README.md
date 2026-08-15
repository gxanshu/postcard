<div align="center">
  <img src="data/icons/hicolor/128x128/apps/in.gxanshu.postcard.png" width="96" alt="Postcard icon">

  # Postcard

  **Geary's three-pane email, rebuilt on GTK 4 without twelve years of accumulated complexity.**

  [![Version](https://img.shields.io/badge/version-1.9.1-3584e4?style=flat-square)](#installing)
  [![Tests](https://img.shields.io/github/actions/workflow/status/gxanshu/postcard/tests.yml?style=flat-square&label=tests)](https://github.com/gxanshu/postcard/actions/workflows/tests.yml)
  [![License](https://img.shields.io/badge/license-GPL--3.0--or--later-3584e4?style=flat-square)](COPYING)
  [![Flatpak](https://img.shields.io/badge/install-Flatpak-4a86cf?style=flat-square&logo=flatpak&logoColor=white)](#installing)

  [**Install**](#installing) · [**Providers**](#supported-providers) · [**Features**](#features) · [**Build**](#building-from-source)
</div>

Folders, conversations, reading pane — the layout you know, on a modern stack: GTK 4, libadwaita,
Python. Small codebase, clean UI, shipped as a Flatpak.

**No accounts, no telemetry, no cloud.** Your mail lives in a SQLite file on your machine, your
passwords in the system keyring. Postcard talks to your mail server and nothing else.

<div align="center">
  <img src="data/screenshots/main-window.png" alt="Postcard showing the three-pane layout with folders, conversations, and reader">
</div>

<table>
  <tr>
    <td width="50%"><img src="data/screenshots/preferences.png" alt="Preferences showing notification, remote image, avatar, background and signature settings"></td>
    <td width="50%"><img src="data/screenshots/about.png" alt="The About dialog showing Postcard version 1.6.0"></td>
  </tr>
  <tr>
    <td align="center"><sub><b>Preferences</b>: notifications, remote images, background sync, signature</sub></td>
    <td align="center"><sub><b>About</b>: plain libadwaita, no custom chrome</sub></td>
  </tr>
</table>

<div align="center">
  <img src="data/screenshots/mail-compose.png" width="340" alt="The composer at a narrow window size, with To/Cc/Bcc fields and a rich-text toolbar">

  <sub><b>Composer</b>: rich text, and the layout adapts down to a phone-width window</sub>
</div>

<div align="center">
  <img src="data/screenshots/online-accounts.png" alt="The Online Accounts dialog listing a Google account from GNOME Settings, ready to add">

  <sub><b>Online Accounts</b>: add a Google account with one click, no app password to generate</sub>
</div>

> **Heavy development.** Expect bugs. If you hit one, please
> [report it](https://github.com/gxanshu/postcard/issues) — it helps a lot.

## Features

**📬 Mail**

- **Google in one click** — pick the account you already signed in to under GNOME Settings →
  Online Accounts. OAuth, so Postcard never sees a password.
- **Any IMAP/SMTP account**, as many as you like. Passwords go to the system keyring, and you pick
  TLS or STARTTLS per server (so Proton Mail Bridge works).
- **Server settings filled in for you** from your address — Gmail, Yahoo, iCloud, Outlook,
  Fastmail, Zoho, AOL, Yandex. Anything you type by hand is left alone.
- **Threaded conversations** and nested folders shown as a tree.
- **Full-text search** across all your mail.
- **Works offline** from a local cache, and keeps syncing after you close the window.

**✍️ Composing**

- **Rich text** — bold, italic, underline, strikethrough, bulleted and numbered lists, links. Sent
  as HTML with a plain-text alternative.
- **Reply, reply-all, forward**, with Cc/Bcc, a signature, and a Drafts/Outbox that never loses a
  message.
- **Recipient autocomplete** from the addresses already in your mail.

**🖥️ Desktop**

- **HTML and plain-text mail**, with remote images blocked until you allow them and links opening
  in your browser.
- **Archive, trash, move, undo** — on one conversation or a whole selection.
- **Attachments** open in their default app with one click.
- **Relative dates** ("2h ago", "Yesterday"), exact timestamp on hover.
- **Desktop notifications** when new mail arrives.
- **Sender avatars** from Gravatar, falling back to the sender's site icon. Optional.
- **Default mail client** — `mailto:` links open the composer already filled in.

More coming.

## Supported providers

Postcard speaks plain IMAP and SMTP, so anything that does works too. **Works** means the protocol
is supported; **Tested** means it's been run against a real account.

| Provider | Works | Tested | Notes |
|---|:---:|:---:|---|
| **Gmail** | ✅ | ✅ | Easiest way in: add your Google account in GNOME Settings → Online Accounts, then pick it in Postcard. By hand also works, but needs 2-Step Verification and an [app password](https://myaccount.google.com/apppasswords) |
| **Yahoo Mail** | ✅ | ✅ | Needs an app password from Account Security |
| **Proton Mail** | ✅ | ✅ | Via [Proton Mail Bridge](https://proton.me/mail/bridge) (paid plans) — use the Bridge's local host, port, and password with STARTTLS |
| **Any IMAP/SMTP server** | ✅ | — | Fastmail, Zoho, Mailbox.org, Migadu, self-hosted Dovecot/Postfix. Enter host, port, and TLS mode by hand |
| **Outlook / Hotmail / Microsoft 365** | ❌ | ❌ | Microsoft retired basic auth and requires OAuth 2.0. Planned |

> **Tip:** most providers with 2FA reject your account password over IMAP. Generate an app-specific
> password instead — or, for Google, use Online Accounts and let Postcard sign in with OAuth.

Only OAuth accounts are imported from Online Accounts, which in practice means Google. A GOA "Email
Server" account is plain IMAP/SMTP that Add Account already handles, and Microsoft 365 / Exchange
hand out Graph-only tokens with no IMAP access.

## Installing

You'll need `flatpak` (most GNOME distros ship it; otherwise see
[flatpak.org/setup](https://flatpak.org/setup/)). One command — the repository is added for you:

```bash
flatpak install --from https://github.com/gxanshu/postcard/releases/latest/download/postcard.flatpakref
flatpak run in.gxanshu.postcard
```

Prefer clicking? Download
[`postcard.flatpakref`](https://github.com/gxanshu/postcard/releases/latest/download/postcard.flatpakref)
and open it with your software centre.

Postcard then shows up in your app launcher like any other app, and `flatpak update` keeps it
current.

## Starting hidden at login

Turn on **Keep running in the background** in Preferences, then autostart:

```bash
flatpak run in.gxanshu.postcard --hidden
```

GNOME Tweaks can only pick existing applications, so if it won't take the flag, write
`~/.config/autostart/in.gxanshu.postcard.desktop` yourself:

```ini
[Desktop Entry]
Type=Application
Name=Postcard
Exec=flatpak run in.gxanshu.postcard --hidden
Icon=in.gxanshu.postcard
```

Postcard starts with no window, checks for mail on your sync interval, and notifies you when
something arrives. Click the notification to open it.

## Building from source

Postcard builds and runs entirely as a Flatpak, the same way it ships. No host-level
`python app.py` — everything goes through [`just`](https://github.com/casey/just):

```bash
just init      # one-time: add Flathub, install the GNOME runtime + SDK
just build     # build from the working tree, install for --user
just run       # build, then launch (the normal dev loop)
```

You need `flatpak` and `flatpak-builder` on the host. Python, GTK, and the rest come from the
GNOME SDK.

## Tech stack

GTK 4, libadwaita, Blueprint (`.blp`) UI, WebKitGTK for HTML mail, SQLite with FTS5 for search,
stdlib `imaplib`/`smtplib` for networking, libsecret for credentials.

## AI Notice

Postcard is written with the help of AI tools. AI does the typing. The architecture, the review,
and the responsibility are mine. Every line was read before it shipped.

Judge that against the repo rather than taking my word for it:

- **175 tests** over the pure-logic layer — threading, MIME parsing, compose, the database, and the
  IMAP/SMTP sessions against a fake socket.
- **CI runs them on every push** and again before every release. A failing test blocks the build,
  not just the merge.
- **`ruff` + `pyright` gate the Flatpak itself.** A lint or type error means the app doesn't build,
  so there's no "it typechecks later" drift.
- **Documented architecture with real invariants** ([CLAUDE.md](CLAUDE.md)): a hard no-GTK-in-`core/`
  boundary, a strict worker-thread/`GLib.idle_add` model, and passwords that never touch the
  database or a log line.
- **Zero third-party runtime dependencies.** Everything comes from the GNOME SDK. No dependency pile
  to hide sloppiness in.
- **GPL-3.0, under 7k lines.** Small enough to read the whole thing and decide for yourself.

<details>
<summary>Why I use AI, and why the app isn't on Flathub</summary>

<br>

The beauty of Linux is that everyone is free. Much of the Flathub and GNOME community is wary of AI,
and I respect that view. Flathub declined to list Postcard on those grounds, which is why it ships
from its own repository. I just happen to see it differently — AI is a tool like fire: put to good
use, it's a wonderful thing.

I'm not interested in spending hours typing out code that's already fully formed in my head, so I
let AI do the typing. But I'd never recommend running it on autopilot. You have to stay in control
of what it produces, and the checks above exist because I don't trust it any further than I'd trust
myself at 2am.

If this still reads as "AI slop" to you, that's fair, and you're welcome to use whichever client
suits you. But if you do install Postcard, I hope you'll trust it. It's built with the same care as
anything written by hand.

</details>

## Contributing

Contributions are welcome — bug reports especially, since Postcard is young and every real inbox is
different. Code should pass `just check` and `just test`. AI-assisted work is fine (see
[AI Notice](#ai-notice)), as long as you understand every line you submit.

New here? [CONTRIBUTING.md](CONTRIBUTING.md) covers the dev environment — either open the folder in
[GNOME Builder](https://flathub.org/en/apps/org.gnome.Builder) and let it install the SDK it
suggests, or run `just init` / `just run`.

If Postcard is useful to you, a ⭐ helps other people find it.

## License

[GPL-3.0-or-later](COPYING).
