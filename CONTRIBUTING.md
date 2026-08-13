# Contributing to Postcard

Thanks for taking a look! This covers how to get a dev environment running.
For the project's architecture and conventions, see [CLAUDE.md](CLAUDE.md).

## Easiest way: GNOME Builder

1. Install [GNOME Builder](https://flathub.org/en/apps/org.gnome.Builder) from Flathub.
2. Open this repo's folder in Builder.
3. Builder reads `in.gxanshu.postcard.json` and offers to install the SDK it
   recommends — accept it.
4. Hit the ▶ Run button.

That's it — no terminal needed. Builder builds and runs the Flatpak for you on
every change.

## The terminal way: `just`

Everything else goes through [`just`](https://github.com/casey/just) as a
thin wrapper around the same Flatpak toolchain Builder uses. There is no
host-level `python app.py` — Python, GTK, and meson all come from the GNOME
SDK. You'll need `flatpak` and `flatpak-builder` installed on your system.

```bash
just init      # one-time: add Flathub, install the GNOME runtime + SDK
just run       # build from your working tree and launch it — the normal dev loop
```

`just init` only needs to run once per machine. After that, `just run` is the
loop: it rebuilds from whatever is in your working tree (uncommitted edits
included) and launches the app.

A few more recipes worth knowing:

```bash
just build     # build + install, without launching
just run-debug # run with G_MESSAGES_DEBUG=all
just inspect   # run with the GTK Inspector open
just check     # ruff check + ruff format --check + pyright
just test      # run the test suite (also runs `check` first)
just fmt       # auto-format with ruff
just bundle    # produce a single-file postcard-<version>.flatpak
```

Run `just` with no arguments any time to see the full recipe list.

## Before you submit

Code contributions should pass `just check` and `just test` — `just build`
and `just run` depend on both, so a lint, type, or test failure blocks the
Flatpak build itself, not just CI.
