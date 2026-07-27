# Postcard — task runner (Flatpak-first)
# Run `just` with no args to see all recipes.
#
# Postcard is built and run as a Flatpak — the exact same way it ships to users.
#   just init    (one time)  install the GNOME runtime + SDK this app needs
#   just build               build the Flatpak from your working tree
#   just run                 build, then launch it in the Flatpak sandbox
#
# The Flatpak build compiles your CURRENT working tree (uncommitted edits
# included) — the manifest uses a "dir" source, so no need to commit first.
#
# Requires: flatpak, flatpak-builder. (meson/ninja/python come from the SDK.)

app-id   := "in.gxanshu.postcard"
manifest := app-id + ".json"

# Read straight from meson.build so the bundle filename always matches the
# version in the metainfo release entry — no separate place to update.
version  := `grep -m1 "version:" meson.build | sed -E "s/.*'([^']+)'.*/\1/"`

# flatpak-builder writes its build tree + local repo here (kept out of git).
fp-builddir := ".flatpak/build"
fp-repo     := ".flatpak/repo"
bundle      := "postcard-" + version + ".flatpak"

# A plain host meson build dir, used ONLY by `pot` to regenerate translations.
# The app is never *built* or *run* from here — see `build`/`run`.
builddir := "build"

python := if path_exists(".venv/bin/python") == "true" { ".venv/bin/python" } else { "python3" }

# Show the recipe list (default when you just run `just`).
default:
    @just --list

# ----------------------------------------------------------------------------
# First-time setup
# ----------------------------------------------------------------------------

# One-time: add Flathub and install the runtime + SDK the manifest asks for.
init:
    flatpak remote-add --if-not-exists --user \
        flathub https://flathub.org/repo/flathub.flatpakrepo
    flatpak-builder --install-deps-from=flathub --install-deps-only --user \
        "{{fp-builddir}}" "{{manifest}}"

# ----------------------------------------------------------------------------
# Build & run (Flatpak)
# ----------------------------------------------------------------------------

# Build Postcard as a Flatpak from your working tree and install it for your user.
# --disable-updates: trust already-cloned sources (e.g. blueprint-compiler) instead
# of re-fetching from upstream on every build. Drop it if you bump a source's tag/commit.
build: test
    flatpak-builder --force-clean --user --install --disable-updates \
        "{{fp-builddir}}" "{{manifest}}"

# Build, then launch the Flatpak. This is the normal way to run Postcard.
run: build
    flatpak run "{{app-id}}"

# Run with verbose GLib logging (handy for debugging signals/lifecycle).
run-debug: build
    flatpak run --env=G_MESSAGES_DEBUG=all "{{app-id}}"

# Run with the GTK Inspector open (Ctrl+Shift+D also toggles it at runtime).
inspect: build
    flatpak run --env=GTK_DEBUG=interactive "{{app-id}}"

# ----------------------------------------------------------------------------
# Package & lint
# ----------------------------------------------------------------------------

# Build, export to a local repo, and produce a single-file .flatpak bundle.
bundle: test
    flatpak-builder --force-clean --disable-updates --repo="{{fp-repo}}" \
        "{{fp-builddir}}" "{{manifest}}"
    flatpak build-bundle "{{fp-repo}}" "{{bundle}}" "{{app-id}}"
    @echo "Wrote {{bundle}} — install with: flatpak install --user {{bundle}}"

# Lint the manifest (needs org.flatpak.Builder: flatpak install flathub org.flatpak.Builder).
lint:
    flatpak run --command=flatpak-builder-lint org.flatpak.Builder manifest "{{manifest}}" \
        || echo "flatpak-builder-lint not installed (flatpak install flathub org.flatpak.Builder)"

# ----------------------------------------------------------------------------
# Editor tooling & housekeeping
# ----------------------------------------------------------------------------

# Format the codebase with ruff.
fmt:
    ruff format src tests

test *ARGS:
    {{python}} -m pytest {{ARGS}}

# Regenerate the .pot translation template. Opt-in dev tool: needs `meson`,
# `ninja`, and `gettext` on the host (not required for `build`/`run`).
pot:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -d "{{builddir}}" ]; then
        meson setup "{{builddir}}"
    else
        meson setup --reconfigure "{{builddir}}"
    fi
    ninja -C "{{builddir}}" postcard-pot

# Remove all build artifacts (flatpak + meson + bundle).
clean:
    rm -rf "{{builddir}}" ".flatpak" ".flatpak-builder" postcard-*.flatpak
