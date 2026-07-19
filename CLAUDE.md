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
just lint      # flatpak-builder-lint on the manifest
```

Requires `flatpak` + `flatpak-builder` on the host; Python/GTK/meson come from the GNOME SDK. `just build` passes `--disable-updates` to reuse already-cloned sources (e.g. blueprint-compiler) — drop it if you bump a source's tag/commit in `in.gxanshu.postcard.json`.

## Lint, format, tests

- **Format:** `just fmt` (ruff, line length 88, py312 target). Editor config in `pyproject.toml`; Zed formats on save.
- **Tests:** the GTK-free `core/` modules are unit-tested with `pytest`; tests live under `tests/` mirroring the `core/` package layout (e.g. `tests/core/test_threader.py`). Note: the checked-in `.venv` is dev-tooling only (per `pyproject.toml`) and its interpreter path breaks after the project rename — recreate it or use a system pytest. Only the pure-Python `core/` code (threader, compose, mime parser, models, database) is meaningfully testable without a display.
- `pyright` runs in `basic` mode over `src/postcard`.

## Architecture

Two layers, and the boundary matters:

- **`src/postcard/core/`** — UI-agnostic logic, no GTK widgets. Sub-packages: `models/` (dataclasses: Account, Folder, Email, Conversation, Attachment), `store/database.py` (all SQLite), `net/` (`imap_session.py`, `smtp_session.py`, `errors.py` — thin stdlib `imaplib`/`smtplib` wrappers), `mime/message_parser.py`, plus `threader.py`, `compose.py`, `secrets.py`. This is where testable logic belongs.
- **`src/postcard/*.py`** — the GTK layer. `application.py` (Adw.Application, app actions/accelerators), `window.py` (the ~1300-line main window and the bulk of the UI), and per-view modules (`composer_window.py`, `message_view.py`, `conversation_row.py`, dialogs, `mail_sync.py`, `mail_send.py`).

### Threading model (critical)

**All network I/O runs on a `threading.Thread(daemon=True)`; results are marshalled back to the main thread with `GLib.idle_add`.** The worker function does network only — it must never touch the database or GTK widgets. The `_on_*` callback that `idle_add` schedules runs on the main loop and is the only place that mutates the DB or UI. Follow this pattern for any new network action (see `_start_sync`/`_sync_worker`/`_on_sync_done` in `window.py`). IMAP/SMTP sessions are opened and torn down per operation, not pooled.

### UI is Blueprint, not hand-written XML

UI is authored in `src/ui/*.blp` (Blueprint). At build time meson runs `blueprint-compiler batch-compile` → `.ui` files, which are bundled into a GResource (`src/postcard.gresource.xml`) under the prefix `/in/gxanshu/postcard`. Widgets are wired up with `@Gtk.Template(resource_path="/in/gxanshu/postcard/ui/<name>.ui")` and `Gtk.Template.Child()` — **the Python attribute name must exactly match the `id` in the `.blp` file.** When you add a widget you reference in Python, edit the `.blp` (not the generated `.ui`). New `.blp` files must be registered in **both** `src/meson.build` (the `blueprints` target) and `src/postcard.gresource.xml`; new `.py` files must be added to the appropriate `install_sources` list in `src/meson.build` or they won't ship in the Flatpak.

### Data & sync

- SQLite at `$XDG_DATA_HOME/postcard/postcard.db`, created/migrated in `Database._create_tables`. Full-text search uses an FTS5 virtual table (`emails_fts`) kept in sync via triggers; search goes through `search_conversations`.
- **Conversation threading:** `core/threader.py` union-finds emails by Message-ID / In-Reply-To / References with a normalized-subject fallback; the conversation id is the smallest email id in the group. `Database.reassign_conversations` recomputes and persists it after each sync.
- Message ordering uses the IMAP UID (`server_id`), **not** the local autoincrement id, because load-on-scroll backfill assigns newer local ids to older mail (see `_arrival_key`).
- Folders mirror the server list each sync (`prune_folders`), keeping only the local `Outbox`. Folder role/icon/display-name classification lives in `mail_sync.py` (`role_for_folder`, `icon_for_folder`, `display_name_for_folder`) — matched by name substring, tolerant of casing and Gmail's `[Gmail]/` prefixes.
- Settings are GSettings, schema `in.gxanshu.postcard` in `data/*.gschema.xml` (sync interval, notifications, remote-image loading, signature, window geometry). Add keys there before reading them.
- Passwords are stored in the system keyring via libsecret (`core/secrets.py`), never in the DB.

## Conventions

- App ID `in.gxanshu.postcard`; GResource/GSettings prefix `/in/gxanshu/postcard`. GType names are `Postcard*` (e.g. `PostcardMainWindow`) — keep this prefix when adding templated widgets, and update it everywhere if the app is ever renamed again (it was renamed Postbox → Postcard).
- Commits: Conventional Commits (`feat:`, `fix:`, `chore:`), terse, **no AI/co-author trailers**.
- `gi.require_version()` must run before the matching `gi.repository` import, which forces module-level imports below it; ruff's `E402` is disabled per-file for those modules in `pyproject.toml` — do the same for any new file that needs a `require_version` gate.
- User-facing strings use `gettext` as `_()`; keep new translatable files listed in `po/POTFILES.in` and regenerate the template with `just pot`.
