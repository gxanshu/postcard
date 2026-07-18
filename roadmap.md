# Postbox — Build Roadmap 📬

A step-by-step, beginner-friendly plan to build **Postbox**: a modern email client for
Linux with the **same layout and features as Geary**, but on a clean, current stack
(GTK 4 · libadwaita · Blueprint · Python) and a codebase that stays *simple on purpose*.

You already have the GNOME app template building. This roadmap takes you from that empty
shell to a real email client, one small, satisfying step at a time. Each phase gives you
something you can **run and see**, teaches you **one new concept**, and leaves hints so you
do the figuring-out (that's where the learning is).

> **Read this first, then work top-to-bottom.** Don't skip ahead to IMAP — half the phases
> exist to make the hard phases easy when you get there.

---

## 🧭 Our philosophy — simplicity over complexity

Pin these to the wall. Every time you're unsure, re-read them.

1. **Boring code is good code.** The clever solution you're proud of is the one you'll hate
   in six months. Write the obvious thing.
2. **One file, one job. One class, one job.** If you can't describe a file in a single
   sentence, split it.
3. **The engine never imports GTK.** Mail logic (IMAP, database, parsing) must compile
   without any UI. This one rule keeps the whole project sane. (Geary's biggest win.)
4. **Fake it before you make it.** Build every screen with hardcoded data first. Real data
   comes later, once the screen already works.
5. **Small, running steps.** Never go more than a session or two without the app launching.
   If it doesn't run, you can't learn from it.
6. **Delete more than you add.** When a phase ends, look for code to remove. Less code =
   fewer bugs.
7. **Copy ideas from Geary, not code.** You have Geary's source at
   `/home/anshu/code/projects/geary` — read it to understand *how* a real client solves a
   problem, then write your own simpler version.

---

## 🧱 Tech stack — and what it replaces in Geary

| Concern | We use (modern) | Geary used (old) | Why |
|---|---|---|---|
| Language | **Python (PyGObject)** | Vala | Easiest to learn, no compile step, tons of GNOME examples |
| Build | **Meson + Ninja** | Meson | Already set up in your repo; installs the app + launcher |
| Toolkit | **GTK 4** | GTK 3 | Current, faster, better lists |
| Design layer | **libadwaita 1.4+** | libhandy 1 | Adwaita widgets, responsive split views |
| UI markup | **Blueprint (`.blp`)** | `.ui` XML | Readable UI files — your ask |
| HTML mail render | **WebKitGTK 6.0** (via GI) | webkit2gtk-4.1 | Sandboxed HTML rendering for GTK4 |
| Mail parsing | **GMime 3.0** (via GI) | GMime 3.0 | Parse/build MIME (don't write this yourself) |
| Local storage | **SQLite** (`sqlite3` stdlib) | SQLite | Offline cache of messages |
| Passwords | **libsecret** (`gi Secret`) | libsecret | Store credentials in the keyring, never on disk |
| Networking | **Python stdlib `imaplib` + `smtplib`** | GIO sockets + GTls | Both ship in Python already; they handle the TLS socket, auth, and protocol framing for you |
| OAuth / HTTP | **libsoup 3.0 + json-glib** | libsoup 3 | Gmail/Outlook login (later phases) |

> Everything above except SQLite and networking is reached the same way — through
> **GObject Introspection** (`from gi.repository import ...`). Python's own standard
> library gives you `sqlite3`, `imaplib`, and `smtplib` for free, so there's nothing to
> install for storage or for talking to a mail server.

**Deliberately skipped for simplicity** (add only if you truly want them): `libgee`
(use Python lists + `Gio.ListStore`/`GListModel` instead), `libpeas` plugins, GNOME Online
Accounts. Fewer dependencies = less to learn = less to break.

---

## 🗂️ Target architecture — the shape we're aiming for

Grow into this gradually (Phase 1 sets it up). **The golden rule:** nothing in `core/`
may ever say `using Gtk;`.

```
src/
  postbox.in                # launcher script (meson fills in paths -> bin/postbox)
  postbox.gresource.xml     # bundles the compiled .ui files
  ui/                       # Blueprint (.blp) files live here
  postbox/                  # the Python package (installed under the app's data dir)
    __init__.py
    main.py                 # entry point (given)
    application.py          # app lifecycle + app.* actions only
    window.py               # the 3-pane window
    composer_window.py      # writing an email (separate window)
    conversation_row.py     # a row widget for the conversation list
    account_dialog.py       # add-account flow
    core/                   # ZERO gtk. pure logic + data. testable alone.
      models/
        account.py          # a plain data object
        folder.py
        email.py
        conversation.py
      store/
        database.py         # SQLite read/write (sqlite3 stdlib)
      net/
        imap_session.py     # talk to the mail server (wraps stdlib imaplib)
        smtp_session.py     # send mail (wraps stdlib smtplib)
      mime/
        message_parser.py   # GMime wrapper: bytes -> Email
data/                       # icons, gschema, desktop file (given)
```

> Python modules use `snake_case` (no hyphens) — that's why files are `window.py`, not
> `main-window.py`. The Blueprint `.blp` files keep their dashes; only the code changes.
>
> When `core/` needs to tell the UI something happened ("new mail arrived"), it emits a
> **GObject signal** (`GObject.Signal`). The UI connects to it. Core never imports `Gtk`
> and never calls the UI directly. That's the whole trick to keeping them separate.

---

## ▶️ How to use this roadmap

Every phase follows the same shape:

- **🎯 Goal** — what runs at the end.
- **📚 You'll learn** — the one new concept.
- **🔨 Steps** — the path, not the code.
- **💡 Hints** — API names, widgets, gotchas. Enough to unblock, not enough to spoil.
- **✅ Done when** — how you know to move on.

**Workflow:**
- Make a **git branch or commit per phase** (`git commit -m "Phase 3: local database"`).
- Keep the app **runnable at every commit**.
- At the end of each phase, ask: *"What can I delete or simplify?"*
- Stuck? Read the matching folder in Geary's source for inspiration, then close it and
  write your own.

**Build & run** (from the repo root) — everything happens inside Flatpak, the same way
the app ships to users, so you never install GTK/Python build tools on your host:
```bash
just init                 # first time only: fetch the GNOME runtime + SDK
just build                # build the Flatpak from your working tree
just run                  # build, then launch it
```
(Or just use GNOME Builder — press ▶.)

---

# The Phases

## Phase 0 — Foundations & Blueprint setup
**🎯 Goal:** Same app as today, but UI is written in Blueprint (`.blp`), and you understand
every file the template gave you.

**📚 You'll learn:** How a GNOME app is wired together (Meson → GResource → GTK templates),
and the Blueprint language.

**🔨 Steps:**
1. Read every existing file and write a one-line comment at the top of each explaining its
   job. If you can't, you don't understand it yet — fix that.
2. Add the **Blueprint compiler** to the build so `.blp` files compile to `.ui`
   automatically.
3. Convert `window.ui` → `window.blp` and `shortcuts-dialog.ui` → `.blp`.
4. Rename things to your real structure: `window.py` with class `PostboxMainWindow`
   (`__gtype_name__ = "PostboxMainWindow"`). Update `meson.build` and the gresource file.

**💡 Hints:**
- Install `blueprint-compiler` (it's a Fedora package: `blueprint-compiler`).
- The canonical Meson snippet uses a `custom_target` (or the blueprint subproject's
  `blueprint_compiler`) to turn `*.blp` into `*.ui`, then feed those into
  `gnome.compile_resources`. The Blueprint docs have the exact snippet — copy it once,
  understand each line.
- Blueprint gotcha: the template line is `template $PostboxMainWindow : Adw.ApplicationWindow`
  — the `$` marks your own (non-library) type.
- Keep `resource_base_path` (`/in/gxanshu/postbox`) consistent everywhere or GTK won't find
  your UI at runtime — the #1 beginner error.

**✅ Done when:** App looks identical, but no hand-written `.ui` XML remains and you renamed
cleanly.

---

## Phase 1 — The app shell & the 3-pane layout
**🎯 Goal:** The real Geary silhouette on screen: **folder sidebar | conversation list |
reader**, responsive (collapses to one pane on a narrow window), all with *empty*
placeholders.

**📚 You'll learn:** libadwaita's responsive navigation, and multi-window (`app.*` vs
`win.*` actions).

**🔨 Steps:**
1. Build the layout by **nesting two split views**: an outer one (sidebar = folders,
   content = ...) whose content is an *inner* split view (list = conversations, content =
   reader).
2. Put an `Adw.ToolbarView` + `Adw.HeaderBar` in each pane (each pane has its own header —
   that's the Geary/Adwaita look).
3. Add an `app.new-window` action (Ctrl+N) that opens a second main window. Confirm two
   windows work independently.
4. Add a responsive breakpoint so on a narrow window the panes stack instead of squishing.

**💡 Hints:**
- `Adw.NavigationSplitView` (added in libadwaita 1.4 — exactly your version floor) is the
  widget. Nest one inside the other's content for three columns.
- Each pane is an `Adw.NavigationPage`.
- Responsiveness: `Adw.Breakpoint` on the window, with `setters` that flip
  `collapsed` to `true` under a `max-width` condition.
- Multi-window: the moment you do `new MainWindow(this)` passing the app, GTK tracks it. No
  manual list needed. (See our earlier discussion — `app.*` = global, `win.*` = per-window.)
- Fill panes with `Adw.StatusPage` ("Select a conversation") so it looks intentional while
  empty.

**✅ Done when:** Resizing the window collapses panes gracefully, and Ctrl+N opens a real
second window.

---

## Phase 2 — Static UI with fake data
**🎯 Goal:** All three panes show *believable* content — a hardcoded folder tree, a list of
fake conversations, and a fake email in the reader. **Zero networking.**

**📚 You'll learn:** GTK4 list rendering — the modern `GListModel` + `Gtk.ListView` +
`Gtk.SignalListItemFactory` pattern, which you'll reuse everywhere.

**🔨 Steps:**
1. Make throwaway plain objects: a `FakeFolder`, a `FakeEmail` with sender/subject/preview/
   date.
2. Folder sidebar: small and fixed → a `Gtk.ListBox` is fine and simplest.
3. Conversation list: potentially thousands of rows → use `Gtk.ListView` backed by a
   `GListStore`. Build each row with a factory (avatar, sender, subject, date, unread dot).
4. Reader: when a row is selected, show its fields in the right pane.
5. Wire selection: click folder → (later) filter list; click conversation → show in reader.

**💡 Hints:**
- `GListStore` holds `GObject`s. Make your row model a small `GObject` subclass with
  properties — that's what the factory binds to.
- Factory has two callbacks: **setup** (build empty widgets once) and **bind** (fill them
  from the item). Keep setup cheap; it's reused as you scroll.
- Row layout: an `Adw.ActionRow` won't give you full control — build a custom `Gtk.Box` in
  the factory for the conversation row.
- Don't fetch avatars from the network — use `Adw.Avatar` with initials for now.
- This is the phase where you *feel* the app. Make it look nice; it's motivating.

**✅ Done when:** You can click through fake folders and conversations and the reader
updates — it looks like an email client, just with pretend data.

---

## Phase 3 — Real data models & local database
**🎯 Goal:** Replace fake objects with proper `core/` models, stored in a local SQLite
database. The UI now reads from the database (still no network — you'll insert test rows by
hand).

**📚 You'll learn:** Designing simple data models, and reading/writing SQLite with Python's
`sqlite3` — your offline foundation. This is the first *engine* code.

**🔨 Steps:**
1. Define clean models in `core/models/`: `Account`, `Folder`, `Email`, `Conversation`.
   Keep them dumb — data + maybe a helper method, no logic.
2. Write `core/store/database.py`: open a DB in the user data dir, create tables
   (accounts, folders, emails), expose simple methods like `save_email`, `emails_in_folder`.
3. On startup, open the DB. Seed it with a few test emails via a temporary function.
4. Point the UI at the database instead of the fake arrays.

**💡 Hints:**
- Python's standard library has `sqlite3` built in (`sqlite3.connect(path)`). No ORM —
  write plain SQL. It's simpler and you'll actually understand your storage.
- Put the DB at `GLib.get_user_data_dir()/postbox/postbox.db`.
- Schema tip: give every email a stable server id + a `folder_id` foreign key. Keep it
  minimal — you can add columns later; migrations for a solo project can be "bump a
  `user_version` and ALTER".
- **Don't** block the UI on DB calls that could be slow — but for now they're tiny, so keep
  it synchronous and simple. (Async comes in Phase 5 where it matters.)
- Read Geary's `src/engine/db` for ideas on structure, but yours should be ~10× smaller.

**✅ Done when:** You delete all the `Fake*` classes (`fake_data.py`) and the app shows
rows that came from SQLite.

---

## Phase 4 — Accounts: add an account & store the password
**🎯 Goal:** A proper "Add Account" flow (like Geary's accounts editor) where the user
enters email + IMAP/SMTP server + password, saved safely. No connection yet — just capture
and store.

**📚 You'll learn:** `Adw` dialogs & forms, form validation, and **libsecret** (never store
a password in your database or a config file).

**🔨 Steps:**
1. Build an account dialog in Blueprint: email, display name, IMAP host/port, SMTP host/port,
   password. Use `Adw.EntryRow` / `Adw.PasswordEntryRow` inside `Adw.PreferencesGroup`s.
2. Save non-secret fields to the DB (`accounts` table); save the **password to the keyring**
   via libsecret, keyed by account id.
3. Show accounts in a list; allow removing one (also clears its secret).
4. Handle "no accounts yet" with an `Adw.StatusPage` + "Add Account" button as the app's
   first-run screen.

**💡 Hints:**
- `Adw.PasswordEntryRow` masks input for free.
- libsecret via GI: `from gi.repository import Secret`, then
  `Secret.password_store_sync` / `Secret.password_lookup_sync` with a `Secret.Schema` you
  define once (attributes like `account`, `server`).
- Prefill common providers later (Gmail = imap.gmail.com:993) — but ship manual entry first;
  it's simplest and works with any provider.
- **Provider tip for learning:** pick a test account on a provider that allows plain
  IMAP + app-passwords (e.g. a self-host, Migadu, Fastmail). This lets you skip OAuth until
  Phase 11. Gmail/Outlook require OAuth2 — a known hard hurdle, deliberately deferred.

**✅ Done when:** You add an account, restart the app, and it's still there — with the
password in the system keyring (check with Seahorse/`secret-tool`), not in your DB.

---

## Phase 5 — IMAP: fetch real mail 🌐 (the big one)
**🎯 Goal:** Connect to the real server, download the folder list and recent message headers
into your database. The conversation list now shows **your actual inbox**.

**📚 You'll learn:** the IMAP conversation (login → list folders → select a mailbox → fetch
headers by UID), wrapping stdlib `imaplib` behind a small clean class, and **keeping the UI
responsive in Python** (a worker thread that hands results back with `GLib.idle_add`) so the
window never freezes. That threading dance is the part that matters for every GTK app you'll
ever write — go slow, celebrate small wins.

**🔨 Steps (smallest-first, resist doing it all at once):**
1. Open an `imaplib.IMAP4_SSL(host, port)` connection and print the server greeting.
   *That alone is a win.*
2. `login(...)`, then `list()` folders → save to DB → show in sidebar.
3. `select("INBOX", readonly=True)`, then `fetch(...)` the most recent N message **headers**
   (UID, flags, from/subject/date) → save → show in list.
4. Move all of this **off the main thread** so the spinner spins and the window stays
   responsive.
5. Add a manual "Refresh" button before you attempt any automatic syncing.

**💡 Hints:**
- `imaplib` is standard library — zero install, ships with your Flatpak's Python already.
  It handles the TLS socket, certificate checks, command tagging, and the fiddly "literal"
  byte-blocks headers arrive in, so you don't hand-parse the wire protocol.
- Stay **read-only**: `select(mailbox, readonly=True)` plus `BODY.PEEK[...]` in your fetch
  request means "look without touching" — a plain writable select or `BODY[...]` would mark
  messages as read on the server just by glancing at them.
- Use the message **UID** (stable) as your DB's `server_id`, not the message *number*
  (1, 2, 3…), which shifts as mail arrives/leaves. `SELECT` reports how many messages exist,
  so fetch the range `count-N+1 : count` for "the newest N."
- Staying responsive in Python: the reliable pattern is a `threading.Thread` doing the
  blocking `imaplib` calls, then `GLib.idle_add(callback, result)` to touch widgets back on
  the main thread (GTK is **not** thread-safe — only the main thread may call into it).
  Learn this pattern here and reuse it forever.
- Keep `imaplib` wrapped behind a small class in `core/net/imap_session.py` exposing just
  `connect/login/list_folders/select/fetch_recent_headers/logout` — nothing else in the app
  should need to know you're using `imaplib`. When mail arrives, hand back plain data (or
  emit a signal); the UI never touches the socket layer directly. (Golden rule again.)
- Save-then-display: always write to the DB, then let the UI read the DB. Never let a widget
  hold the only copy of your mail.
- Want to *see* the protocol anyway, just to understand what `imaplib` is doing for you? Set
  `imaplib.Debug = 4` temporarily — it prints the whole conversation to your terminal.

**✅ Done when:** You launch Postbox and see today's real inbox headers, fetched live, with
the UI staying smooth.

---

## Phase 6 — Reading mail: MIME, HTML & attachments
**🎯 Goal:** Click a message → fetch its full body → render it properly (plain text *and*
HTML email), with attachments listed and saveable. This is Geary's `conversation-viewer`.

**📚 You'll learn:** MIME structure (why email bodies are so weird), parsing with **GMime**,
and safely rendering HTML with **WebKitGTK**.

**🔨 Steps:**
1. `FETCH` the full raw message (RFC822 body) on demand when a conversation is opened; cache
   it in the DB.
2. Parse it with GMime in `core/mime/message_parser.py` → extract text body, HTML body,
   and attachment parts.
3. Render plain text in a label; render HTML in a `WebKit.WebView`.
4. **Block remote images by default** (privacy — this is a real feature, not optional) with
   an info bar to "Show images". Block loading remote content and JavaScript.
5. List attachments in a panel (`Adw` rows) with a Save button.

**💡 Hints:**
- GMime: `GMime.Parser` → `GMime.Message`; walk parts to find `text/plain`, `text/html`, and
  attachments. Don't try to parse MIME by hand — that way lies madness.
- WebKit: use its `NetworkSession`/settings to disable JavaScript and remote loads; only
  allow them when the user clicks "Show images". Geary's `web-process` and
  `conversation-message` show the security posture to copy.
- Sanitizing HTML email is a genuine security surface. Default to *paranoid*: no scripts, no
  remote fetches, until the user opts in per-message.
- Keep the parser in `core/` returning your own plain `Email`/`Attachment` objects; the
  WebView (UI) only ever sees clean data.

**✅ Done when:** You can read a real HTML newsletter and a plain-text email, images are
blocked until you allow them, and you can save a PDF attachment.

---

## Phase 7 — Composing & sending ✍️
**🎯 Goal:** A composer window (new / reply / forward) that actually sends mail via SMTP,
with a Drafts + Outbox so nothing is lost.

**📚 You'll learn:** SMTP, building MIME messages (the reverse of Phase 6), and a robust
send queue.

**🔨 Steps:**
1. Composer as a **separate window** (`composer_window.py`) — recipients, subject,
   body. Start plain-text; add rich text later.
2. "Reply"/"Forward" prefill it from the open message (quote the original, set To/Subject).
3. Build the outgoing message with GMime, then send it over SMTP using stdlib `smtplib`.
4. Add an **Outbox**: save the message, try to send, keep it queued and retry on failure so a
   dropped connection never loses a mail. Save unfinished mails to **Drafts**.
5. Save sent mail to the Sent folder (append via IMAP or let the server do it).

**💡 Hints:**
- `smtplib` is `imaplib`'s twin — also standard library, also already in your runtime. Use
  `smtplib.SMTP_SSL(host, port)` for implicit TLS (port 465), or `SMTP(host, port)` +
  `starttls()` for port 587. It handles `EHLO`/`AUTH`/`MAIL FROM`/`RCPT TO`/`DATA` for you.
  Put it in `core/net/smtp_session.py`, wrapped the same small-clean-class way as
  `imap_session.py` — and sending happens on a worker thread + `GLib.idle_add`, same dance
  as Phase 5.
- Build MIME with `GMime.Message` + `GMime.Multipart` — the inverse of parsing — then hand
  the resulting bytes to `smtplib.SMTP.sendmail(...)` / `send_message(...)`.
- The Outbox is what makes sending feel reliable — model it as a small table + a queue that
  drains when online. Geary's `src/engine/outbox` is the reference.
- Attachments = add file parts to the multipart. Reuse your Phase 6 attachment model.

**✅ Done when:** You send yourself an email from Postbox, it arrives, and pulling the plug
mid-send leaves it safely in the Outbox to retry.

---

## Phase 8 — Conversations (threading)
**🎯 Goal:** Group related emails into a single conversation thread, like Geary — the
feature that defines its UX.

**📚 You'll learn:** Threading algorithms and how to model one-to-many (conversation → many
emails) cleanly.

**🔨 Steps:**
1. Group messages by their `Message-ID` / `In-Reply-To` / `References` headers (and subject
   as a fallback) into conversations.
2. Store the grouping (a `conversation_id` on each email is the simplest model).
3. Conversation list shows one row per thread (participants, count, latest date).
4. The reader shows the whole thread stacked, newest expanded — collapse older messages.

**💡 Hints:**
- Start dead simple: link by `In-Reply-To`/`References` chains; merge on shared `Message-ID`.
  You do **not** need the full JWZ threading algorithm to start — add sophistication only if
  threads come out wrong.
- Compute this in `core/`, store the result, so the UI just reads `conversation_id`.
- Stacked thread UI: an `Adw.ExpanderRow`-style layout or a `Gtk.Box` of message widgets.

**✅ Done when:** A back-and-forth email exchange shows as one row that expands into the full
thread.

---

## Phase 9 — Search & filtering 🔍
**🎯 Goal:** Geary's fast search: type in the search bar, get matching conversations.

**📚 You'll learn:** Full-text search in SQLite (FTS5) and debounced search UX.

**🔨 Steps:**
1. Add an SQLite **FTS5** virtual table indexing sender, subject, and body; keep it updated
   when you store mail.
2. Add a search entry (`Gtk.SearchBar` + `Gtk.SearchEntry`) toggled from the header.
3. Query FTS as the user types (debounced ~200ms) and show results in the existing list.
4. Add basic folder filtering (unread only, flagged) via the same list model.

**💡 Hints:**
- FTS5 gives you ranked matching almost for free — much simpler than `LIKE '%...%'` and far
  faster.
- Debounce with a `GLib.Timeout` you reset on each keystroke — don't hit the DB on every
  letter.
- Reuse your Phase 2 list model; just swap what feeds it. This is why we built it well early.

**✅ Done when:** Typing a word instantly filters to matching conversations across the
mailbox.

---

## Phase 10 — Mail actions & notifications
**🎯 Goal:** All the verbs: mark read/unread, star/flag, archive, delete (trash), move to
folder — plus desktop notifications for new mail. The client becomes *usable daily*.

**📚 You'll learn:** Mapping UI actions → IMAP commands with **optimistic updates**, and
desktop integration.

**🔨 Steps:**
1. Wire toolbar buttons + right-click menus + swipe/keyboard to `win.*` actions
   (`win.archive`, `win.trash`, `win.mark-read`, `win.star`, `win.move`).
2. Each action: update the DB + UI **immediately** (optimistic), then send the IMAP command
   (`STORE` flags, `MOVE`/`COPY`+`EXPUNGE`); roll back if the server rejects.
3. Undo: show an `Adw.Toast` with "Undo" after destructive actions (very Geary).
4. New-mail desktop notifications via `GLib.Notification` (respect a preference + focus).
5. Keyboard shortcuts for every common action + a shortcuts window.

**💡 Hints:**
- Optimistic UI is what makes it feel instant — apply locally first, reconcile with the
  server after. Keep a tiny "pending ops" idea so you can undo.
- `GLib.Notification` + `application.send_notification()` is the whole notification API.
- Map keys with `set_accels_for_action`. Group them logically (Geary's shortcuts are a good
  crib sheet).

**✅ Done when:** You can run your inbox for a day — read, archive, star, delete with undo —
and get notified of new mail.

---

## Phase 11 — Polish, preferences & the hard edges
**🎯 Goal:** The things that separate a demo from a real app.

**📚 You'll learn:** Settings with GSettings, OAuth2, background sync, and graceful failure.

**🔨 Steps (pick in any order — each is independent):**
1. **Preferences** window (`Adw.PreferencesDialog`) backed by your **GSettings** schema
   (you already have `gschema.xml`): signature, notifications, show-images default, etc.
2. **OAuth2** for Gmail/Outlook (libsoup + json-glib): the modern login flow, so you're not
   limited to app-passwords. This is advanced — do it once the rest is solid.
3. **Background sync**: periodic refresh + IMAP `IDLE` for push. Handle going offline/online
   without crashing.
4. **Error handling everywhere**: wrong password, server down, TLS error → show a friendly
   `Adw` banner/dialog, never a silent failure or a crash.
5. **Empty/loading states**, spinners, and a first-run welcome.

**💡 Hints:**
- GSettings: define keys in your existing `data/in.gxanshu.postbox.gschema.xml`, bind them to
  widgets with `settings.bind(...)` — two-way binding, almost no code.
- IMAP `IDLE` lets the server push new-mail notices instead of you polling — big battery/UX
  win, but add it only after polling works.
- OAuth2 is genuinely the fiddly part of email in 2026. Budget real time; read the provider
  docs; store refresh tokens in libsecret like passwords.
- Treat every network call as "will fail eventually." Wrap, catch, tell the user calmly.

**✅ Done when:** Preferences persist across restarts, a wrong password shows a helpful
message (not a crash), and (stretch) you can log into Gmail.

---

## Phase 12 — Ship it 🚀
**🎯 Goal:** Postbox installs like a real GNOME app and is ready for others.

**📚 You'll learn:** Flatpak packaging and release hygiene.

**🔨 Steps:**
1. Finish the **Flatpak manifest** (`in.gxanshu.postbox.json`) — declare all runtime deps.
2. Complete the **AppStream metainfo** (`data/*.metainfo.xml.in`): screenshots, description,
   release notes — required for stores.
3. Verify the **desktop file** and icons (you have both).
4. Write a real `README.md` (what it is, build steps, screenshot).
5. (Stretch) Submit to **Flathub**.

**💡 Hints:**
- `flatpak-builder build-dir in.gxanshu.postbox.json` builds it; test in the sandbox before
  sharing.
- Bump `version` in the About dialog, metainfo, and a git tag together each release.
- Screenshots in metainfo are what make people try it — take nice ones.

**✅ Done when:** A fresh machine can `flatpak install` Postbox and read email.

---

## 📎 Appendix A — Concepts to learn (and where)

Learn these *as they come up* in the phases above, not all upfront.

| Concept | Phase | One-liner | Where to look |
|---|---|---|---|
| Python + PyGObject basics | 0 | Python with `from gi.repository import ...` | PyGObject docs, GNOME Python tutorial |
| GObject & properties | 2 | The object system everything uses | PyGObject GObject guide |
| GTK templates | 0 | `.ui`/`.blp` bound to a `@Gtk.Template` class | PyGObject + GTK4 docs |
| Blueprint | 0 | Readable UI markup | Blueprint compiler docs |
| Actions (`app.`/`win.`) | 1 | Decoupled commands for menus/keys | GAction docs |
| `Adw.NavigationSplitView` | 1 | Responsive multi-pane | libadwaita docs |
| `GListModel` + `ListView` | 2 | Scalable lists | GTK4 list widget guide |
| SQLite (`sqlite3` stdlib) | 3 | Plain SQL, no ORM | sqlite.org + Python `sqlite3` docs |
| libsecret | 4 | Keyring password storage | libsecret docs |
| Threads + `GLib.idle_add` | 5 | Non-blocking I/O without freezing the UI | PyGObject threading guide |
| IMAP + `imaplib` | 5 | Reading mail via stdlib's IMAP client | Python `imaplib` docs, RFC 3501 |
| GMime / MIME | 6 | Parsing & building mail | GMime docs |
| WebKitGTK | 6 | Rendering HTML mail | WebKitGTK docs |
| SMTP + `smtplib` | 7 | Sending mail via stdlib's SMTP client | Python `smtplib` docs, RFC 5321 |
| SQLite FTS5 | 9 | Full-text search | sqlite.org/fts5 |
| GSettings | 11 | Persisted preferences | GSettings docs |
| OAuth2 | 11 | Modern provider login | provider dev docs |
| Flatpak | 12 | Packaging | docs.flatpak.org |

---

## 📋 Appendix B — Geary feature checklist

Tick these off to know you've matched Geary. (Phase that delivers it in parentheses.)

- [ ] 3-pane responsive layout, per-pane headers (1)
- [ ] Multiple windows (1)
- [ ] Folder sidebar (2, 5)
- [ ] Conversation list with unread/flag indicators (2, 8)
- [ ] Multiple accounts (4)
- [ ] Passwords in keyring (4)
- [ ] Live IMAP inbox (5)
- [ ] Offline cache / reads from local DB (3, 5)
- [ ] HTML + plain-text reading (6)
- [ ] Remote-image blocking with opt-in (6)
- [ ] Attachments: view & save (6)
- [ ] Compose / reply / forward (7)
- [ ] Rich-text composer (7, stretch)
- [ ] Drafts & Outbox with retry (7)
- [ ] SMTP sending (7)
- [ ] Conversation threading (8)
- [ ] Full-text search (9)
- [ ] Archive / trash / move / flag / mark-read (10)
- [ ] Undo via toast (10)
- [ ] New-mail notifications (10)
- [ ] Keyboard shortcuts + shortcuts window (10)
- [ ] Preferences (11)
- [ ] OAuth2 (Gmail/Outlook) (11)
- [ ] Background sync / IMAP IDLE (11)
- [ ] Flatpak on Flathub (12)

---

## 🐞 Appendix C — Gotchas & debugging survival kit

- **UI not showing / "type not found":** almost always a mismatch between the Blueprint
  `template` class name, the Python class's `__gtype_name__`, or the gresource path. Check
  all three.
- **App freezes during network calls:** you did blocking I/O on the main thread. Blocking
  the main loop freezes the UI — move socket/DB-heavy work to a worker thread and hand
  results back with `GLib.idle_add`.
- **`ValueError: unknown type name` at startup:** your `@Gtk.Template` class
  `__gtype_name__` doesn't match the Blueprint `template $Name` — or the class module was
  never imported before the `.ui` loads. Check both.
- **Widget shows nothing:** in GTK4 you must `set_child`/`append` *and* the parent must be
  visible; also check `hexpand`/`vexpand`.
- **Blaming the server:** run `openssl s_client -connect host:993` (IMAP) to talk to the
  server by hand and see the raw protocol — invaluable for Phases 5 & 7.
- **Print-debug freely:** `print(...)` or Python's `logging` go to the terminal. Run from a
  terminal (not just Builder) to see them.
- **Read the reference, don't copy it:** Geary at `/home/anshu/code/projects/geary` is your
  encyclopedia. Understand, then write your own smaller version. Copied code you don't
  understand is a future bug you can't fix.
- **When a phase feels huge:** you're trying to do the whole phase at once. Re-read step 1,
  do only that, run it, commit. Momentum beats perfection.

---

*Build one phase at a time. Keep it running. Keep it simple. You've got this. 📬*
