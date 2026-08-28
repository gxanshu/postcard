# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Postcard is a GTK 4 / libadwaita email client written in Python, built and shipped as a Flatpak.

## Build & run

Everything goes through the Flatpak toolchain via [`just`](justfile) — there is **no host-level `python app.py`**. The Flatpak build compiles the **current working tree, uncommitted edits included** (the manifest uses a `"dir"` source), so you never need to commit to test a change.

```bash
just init      # one-time: add Flathub, install the GNOME 50 runtime + SDK
just build     # build the Flatpak from the working tree, install --user
just run       # build, then launch (the normal dev loop)
just run-debug # run with G_MESSAGES_DEBUG=all
just inspect   # run with the GTK Inspector open
just bundle    # produce a single-file postcard-<version>.flatpak
just check     # ruff check + ruff format --check + pyright
just lint      # flatpak-builder-lint on the manifest
```

Requires `flatpak` + `flatpak-builder` on the host; Python/GTK/meson come from the GNOME SDK. `just build` passes `--disable-updates` to reuse already-cloned sources (e.g. blueprint-compiler) — drop it if you bump a source's tag/commit in `in.gxanshu.postcard.json`.

## Lint, format, tests

- **Format:** `just fmt` (ruff, line length 88, py312 target). Editor config in `pyproject.toml`; Zed formats on save.
- **Tests:** `just test` (host pytest, no Flatpak). `build`, `run` and `bundle` all depend on it, so a failing test blocks the Flatpak build; CI runs the same suite via `.github/workflows/tests.yml`, which `release.yml` calls before publishing. The GTK-free `core/` modules are unit-tested; tests live under `tests/` mirroring the `core/` package layout (e.g. `tests/core/test_threader.py`). `[tool.pytest.ini_options]` sets `pythonpath = ["src"]`, so the package resolves without being installed — no `conftest.py` needed. Note: the checked-in `.venv` is dev-tooling only (per `pyproject.toml`) and `.venv/bin/pytest` has a stale shebang after the project rename, which is why the recipe invokes `.venv/bin/python -m pytest`. Testable without a display: the pure-Python `core/` code (threader, compose, mime parser, models, database), `mail_sync`'s folder classification and its raw-header mapping, and the IMAP/SMTP sessions via a fake in place of `imaplib`/`smtplib` (see `tests/core/net/`). Not testable: `core/secrets.py` (libsecret), the D-Bus half of `core/goa.py` (its pure host/port/security helpers are), and the whole GTK layer, since the Adw typelib only exists inside the Flatpak. That last gap is real — four crashes have shipped in `window.py` code no test could have caught, so exercise those changes with `just run`.
- `pyright` runs in `basic` mode over `src/postcard`. `PyGObject-stubs` must be installed for this to be useful — without it every `gi` import reports as unresolved and drowns the real findings. Install it with `--no-deps`: it declares `PyGObject` as a dependency, which builds `pycairo` from source and needs cairo headers, but the `.venv` deliberately takes `gi` from apt via `--system-site-packages`.
- **Check:** `just check` (ruff check + `ruff format --check` + pyright). `test` depends on `check`, and `build`/`bundle` depend on `test`, so a lint or type error blocks the Flatpak build. CI runs the same three steps.

## Coding standards

The project follows `.claude/skills/coding-standards`, enforced mechanically by `just check` — see the annotated `[tool.ruff.lint]` block in `pyproject.toml`. Rules the standard states that a linter can't check (boolean `is_`/`has_` prefixes, plural collection names, logs that name the resource) are on the reviewer.

Three places the standard is deliberately not applied literally, each with the reason recorded at the site:

- **`TRY003` is disabled.** It forbids long messages outside the exception class, which is in direct conflict with the standard's own logging rule — `ImapError(f"could not move {uid} to {destination}: {data}")` is the message we want, and satisfying `TRY003` means one exception subclass per message.
- **"Fewer than 4 parameters" cannot apply to `core/models/*.py`.** `Account`, `Email` and `Folder` are `GObject.Object` subclasses because `Gio.ListStore` only accepts GObjects, so they can't become dataclasses and every field has to be a constructor parameter. They are keyword-only instead, so call sites still read like a dataclass.
- **"Under 200 lines per file" is the target for `core/` only.** A templated widget class can't be split below its own widget surface without fighting the template, so `@Gtk.Template` modules are measured by whether the split earns its keep rather than by a line count. `window.py` is ~2,200 lines because the alternative was eight files plus a declaration-only ninth — see the main window section below.

### Logging

`logging` is configured once in `main.py` and writes to stderr (journald captures it for the Flatpak). Each module takes `logger = logging.getLogger(__name__)`. Default level is WARNING so a normal run is silent; `POSTCARD_LOG=debug just run` (or any level name) turns it up without a rebuild. This is separate from `G_MESSAGES_DEBUG`, which only affects GLib's own logging.

Every error surfaced to the user as a toast or banner is also logged, because a toast is gone the moment it fades. Log at the worker-thread boundary where the exception is caught, with a message naming the resource — `"could not move %d message(s) from %s to %s (account %s)"`, not `"Error"`. Workers pass the exception itself back to their `idle_add` handler, which turns it into user-facing text via `errors.classify()`; don't interpolate `str(error)` into a toast, it leaks server-verbatim text into the UI.

Never log the `args` tuple of a network worker thread — the account password is a positional item in it (`threading.Thread(args=(..., password, ...))`). CPython tracebacks don't print argument values, so this is safe by default, but any locals-printing formatter would serialize the plaintext password. For the same reason, never set `imaplib.Debug > 4` or call `smtplib.set_debuglevel` — both echo the `LOGIN` command.

## Architecture

Two layers, and the boundary matters:

- **`src/postcard/core/`** — UI-agnostic logic, no GTK widgets. Sub-packages: `models/` (Account, Folder, Email, Conversation, Attachment as `GObject.Object`s so `Gio.ListStore` accepts them, plus `MessageHeader` as a plain dataclass), `store/database.py` (all SQLite), `net/` (`imap_session.py`, `smtp_session.py`, `errors.py` — thin stdlib `imaplib`/`smtplib` wrappers), `mime/message_parser.py`, plus `threader.py`, `compose.py`, `secrets.py`. This is where testable logic belongs.
- **`src/postcard/*.py`** — the GTK layer. `application.py` (Adw.Application, app actions/accelerators), the main window (below), and per-view modules (`composer_window.py`, `message_view.py`, `conversation_row.py`, dialogs, `mail_sync.py`, `tray.py`).

### The main window is one class

`PostcardMainWindow` lives entirely in `window.py` — one `@Gtk.Template` class of
~2,200 lines, ordered by concern and signposted with `# ---` dividers:

| section | what it owns |
|---|---|
| (top) | the template, `__init__`, `_load_mail_view`, window lifecycle |
| accounts | account switcher, adding accounts, opening the composer |
| actions | read/unread, star, the flag worker, row context menu |
| move | archive/trash/move and the undo window |
| folders | the folder sidebar tree, rows, selection |
| list | the conversation list, search, scroll paging |
| reader | the reading pane, message bodies, attachments |
| sync | syncing, the Outbox, the connection banner |

`window_types.py` holds the constants and frozen records the window shares with
`preferences_dialog.py`; it stays a separate module for that reason.

This was eight mixin files plus a 268-line `window_parts.py` that re-declared
every field and cross-mixin method so each mixin would type-check alone. The
declarations were pure overhead — no behaviour, and a second place to update on
every change — so the class was merged back into the thing it always was at
runtime. Keep it that way: splitting it again brings `window_parts.py` back.

The file is long on purpose. Add new window code to the section it belongs to
rather than starting a `window_*.py` module for it.

### Threading model (critical)

**All network I/O runs on a `threading.Thread(daemon=True)`; results are marshalled back to the main thread with `GLib.idle_add`.** The worker function does network only — it must never touch the database or GTK widgets, and that includes reading `self._account`: resolve that on the main thread and pass it in. Credentials are the exception, and deliberately so: `secrets.credential_for` blocks on IPC, and for a GNOME Online Account that can mean a token refresh over the network, so it is called *inside* the worker. Both backends (libsecret, GOA over D-Bus) are thread-safe and touch neither the database nor GTK. The `_on_*` callback that `idle_add` schedules runs on the main loop and is the only place that mutates the DB or UI. Follow this pattern for any new network action (see `_start_sync`/`_sync_worker`/`_on_sync_done` in `window.py`). Hand the worker a frozen snapshot rather than a live object the main thread might mutate — `FlagChange` and `BodyRequest` in `window_types.py` are the examples. IMAP connections are pooled one per account by `mail_sync._pooled_session` — use it
rather than building an `ImapSession`. An operation holds the connection for its whole
run (imaplib is not re-entrant); one that arrives while it is busy opens its own rather
than queueing behind a sync, so a click never waits on a background sync. Every
operation must still `select()` its own mailbox, since a pooled session carries the last
one. `close_sessions()` drops connections — on account removal, on going offline, and
after a failure a caller swallowed rather than raising (see `move_messages`), since the
pool can only discard what it sees fail. SMTP stays per-operation.

### No account is a real state

`_load_mail_view` is skipped entirely when the database has no accounts, so everything it assigns — `_account`, the conversation store, the folder tree — does not exist yet, while background callbacks (the sync timer, `network-changed`, notification actions, accelerators) can still fire. Read that state through a guard clause, never directly. Four separate crashes have come from forgetting this; if you add a method that touches per-account state, check it against a window built on an empty database.

### UI is Blueprint, not hand-written XML

UI is authored in `src/ui/*.blp` (Blueprint). At build time meson runs `blueprint-compiler batch-compile` → `.ui` files, which are bundled into a GResource (`src/postcard.gresource.xml`) under the prefix `/in/gxanshu/postcard`. Widgets are wired up with `@Gtk.Template(resource_path="/in/gxanshu/postcard/ui/<name>.ui")` and `Gtk.Template.Child()` — **the Python attribute name must exactly match the `id` in the `.blp` file.** When you add a widget you reference in Python, edit the `.blp` (not the generated `.ui`). New `.blp` files must be registered in **both** `src/meson.build` (the `blueprints` target) and `src/postcard.gresource.xml`; new `.py` files must be added to the appropriate `install_sources` list in `src/meson.build` or they won't ship in the Flatpak.

### Data & sync

- SQLite at `$XDG_DATA_HOME/postcard/postcard.db`, created/migrated in `Database._create_tables`. Full-text search uses an FTS5 virtual table (`emails_fts`) kept in sync via triggers; search goes through `search_conversations`.
- **Conversation threading:** `core/threader.py` union-finds emails by Message-ID / In-Reply-To / References with a normalized-subject fallback; the conversation id is the smallest email id in the group. `Database.reassign_conversations` recomputes and persists it after each sync.
- Message ordering uses the IMAP UID (`server_id`), **not** the local autoincrement id, because load-on-scroll backfill assigns newer local ids to older mail (see `_arrival_key`).
- Folders mirror the server list each sync (`prune_folders`), keeping only the local `Outbox`. Folder role/icon/display-name classification lives in `mail_sync.py` (`role_for_folder`, `icon_for_folder`, `display_name_for_folder`) — matched by name substring, tolerant of casing and Gmail's `[Gmail]/` prefixes. `role_for_folder` returns a `FolderRole` `StrEnum`; compare against its members rather than bare strings, and note that both `Archive` and Gmail's `All Mail` classify as `ARCHIVE`, which `_folder_with_role` breaks in favour of a real `Archive` folder.
- Settings are GSettings, schema `in.gxanshu.postcard` in `data/*.gschema.xml` (sync interval, notifications, remote-image loading, signature, window geometry). Add keys there before reading them.
- Passwords are stored in the system keyring via libsecret (`core/secrets.py`), never in the DB.

### Credentials come from one of two places

An account is either typed in by hand or imported from GNOME Online Accounts; `accounts.goa_id` is the discriminator, and non-empty means GOA owns the credentials. Everything that needs to sign in calls **`secrets.credential_for(account)`** from its worker thread and hands the `Credential` to the session — never `lookup_password` directly. Both `ImapSession` and `SmtpSession` take one through `sign_in()`, which dispatches on its `mechanism`: `login` for a keyring password, `xoauth2` for a GOA token. Anything else raises rather than leaving the session unauthenticated.

`core/goa.py` reads GOA over raw D-Bus with `Gio.DBusProxy` — `Goa-1.0.typelib` is **not** in the GNOME runtime, only on the host, so `gi.require_version("Goa", "1")` would raise inside the Flatpak. Tokens are fetched live on every operation rather than copied into the keyring, since they expire hourly. This needs `--talk-name=org.gnome.OnlineAccounts` in the manifest, plus `--talk-name=org.gnome.Settings` for the dialog's "Open Online Accounts" button.

**Only OAuth accounts are imported** — in practice Google. A GOA "Email Server" account is plain IMAP/SMTP, which the Add Account dialog already does and tests, so importing it would buy nothing but the typing; `mail_accounts()` flags it `is_oauth2` False and the dialog points at Add Account instead. Microsoft 365 and Exchange come back `is_mail_supported` False: GOA's Microsoft token carries Graph-only scopes (`mail.readwrite`, `mail.send`) with no `IMAP.AccessAsUser.All`, so there is no IMAP server to point at. Neither kind is hidden, so the dialog can always say why a row is unavailable.

### The tray icon

`tray.py` is a StatusNotifierItem plus a minimal `com.canonical.dbusmenu`, hand-rolled over `Gio.DBusConnection` for the same reason `core/goa.py` is (the usual client library, libappindicator, is GTK 3 and not in the runtime). It registers with `org.kde.StatusNotifierWatcher` under the connection's *unique* bus name — a well-known `org.kde.StatusNotifierItem-*` name would need an `--own-name` that Flatpak wildcards can't express — and re-registers whenever the watcher reappears (bar restarts). It shows and hides by flipping `Status` between `Active`/`Passive` with a `NewStatus` signal instead of deregistering, because the watcher only forgets an item when its bus name dies, and ours is the application's. Visibility follows `run-in-background` (wired in `application.py`). The unread badge is the app icon re-rendered with cairo and pushed as `IconPixmap` — SNI has no count field, and hosts prefer `IconName`, so the name is blanked while a badge is up; the count is the sum of inbox-role folder badges, pushed from the end of `_reload_folders`, which every mutation path already runs through. GNOME ships no watcher, so nothing appears there and the Background Apps menu stays the only handle; the manifest needs `--talk-name=org.kde.StatusNotifierWatcher`.

## The website

`web/` is the landing page served at `postcard.gxanshu.in` from the **`pages`** branch.
`sh web/build.sh <dir>` (or `just site`) assembles it and replaces `__VERSION__` with the
`version:` from `meson.build`, so the version on the page can never drift from the release.
Screenshots there are WebP copies of `data/screenshots/*.png`, regenerated with
`magick <src> -resize 1920x -quality 82 web/img/<name>.webp` when a screenshot changes.

Two workflows publish, and the split is not optional:

- `release.yml` publishes with `force_orphan: true`, which **wipes the branch** and rebuilds
  it. The signed ostree `repo/` lives on that branch, so it can only be published together
  with the repo.
- `site.yml` publishes on any push touching `web/**`, with `keep_files: true` so `repo/`
  survives. Anything that deploys the site outside these two paths must keep `repo/` intact
  or every installed copy loses `flatpak update`.

Install buttons must point at
`https://github.com/gxanshu/postcard/releases/latest/download/postcard.flatpakref` and
nowhere else — that URL is what increments GitHub's per-asset download counter, which is the
only install metric the project has. `release.yml` deliberately writes the `.flatpakref`
outside `site/` for the same reason: mirroring it onto the site would leak downloads past the
counter.

## Conventions

- App ID `in.gxanshu.postcard`; GResource/GSettings prefix `/in/gxanshu/postcard`. GType names are `Postcard*` (e.g. `PostcardMainWindow`) — keep this prefix when adding templated widgets, and update it everywhere if the app is ever renamed again (it was renamed Postbox → Postcard).
- Commits: Conventional Commits (`feat:`, `fix:`, `chore:`), terse, **no AI/co-author trailers**.
- `gi.require_version()` must run before the matching `gi.repository` import, which forces module-level imports below it; ruff's `E402` is disabled per-file for those modules in `pyproject.toml` — do the same for any new file that needs a `require_version` gate.
- User-facing strings use `gettext` as `_()`; keep new translatable files listed in `po/POTFILES.in` and regenerate the template with `just pot`.
