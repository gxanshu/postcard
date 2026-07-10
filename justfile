# Postbox — task runner
# Run `just` with no args to see all recipes.
#
# Requires: meson, ninja, flatpak, flatpak-builder.

app-id   := "in.gxanshu.postbox"
manifest := app-id + ".json"
builddir := "build"
bin      := "src/postbox"

# Flatpak build artifacts (kept out of the source tree).
fp-builddir := ".flatpak/build"
fp-repo     := ".flatpak/repo"
bundle      := "postbox.flatpak"

# Show the recipe list (default when you just run `just`).
default:
    @just --list

# ----------------------------------------------------------------------------
# Configure
# ----------------------------------------------------------------------------

# Configure the build dir if it doesn't exist yet (no-op otherwise).
_setup:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -d "{{builddir}}" ]; then
        meson setup "{{builddir}}"
    fi

# Force a fresh reconfigure (use after editing meson.build if things act up).
reconfigure:
    meson setup --reconfigure "{{builddir}}"

# Switch the build dir to an optimized release configuration.
configure-release: _setup
    meson configure "{{builddir}}" -Dbuildtype=release -Ddebug=false -Db_ndebug=true

# Switch the build dir back to a debug configuration.
configure-debug: _setup
    meson configure "{{builddir}}" -Dbuildtype=debug -Ddebug=true -Db_ndebug=false

# ----------------------------------------------------------------------------
# Build
# ----------------------------------------------------------------------------

# Compile (default/current config). Regenerates compile_commands.json for the LSP.
build: _setup
    ninja -C "{{builddir}}"

# Clean debug build with symbols (good for gdb / valgrind).
build-debug: configure-debug
    ninja -C "{{builddir}}"

# Optimized production build.
build-prod: configure-release
    ninja -C "{{builddir}}"

# ----------------------------------------------------------------------------
# Run
# ----------------------------------------------------------------------------

# Build and run uninstalled (meson devenv sets GSETTINGS_SCHEMA_DIR etc.).
run: build
    meson devenv -C "{{builddir}}" "{{bin}}"

# Run with verbose GLib logging (handy for debugging signals/lifecycle).
run-debug: build
    G_MESSAGES_DEBUG=all meson devenv -C "{{builddir}}" "{{bin}}"

# Run with the GTK Inspector open (Ctrl+Shift+D also toggles it at runtime).
inspect: build
    GTK_DEBUG=interactive meson devenv -C "{{builddir}}" "{{bin}}"

# Run under gdb (implies a debug build).
gdb: build-debug
    meson devenv -C "{{builddir}}" gdb "{{bin}}"

# ----------------------------------------------------------------------------
# Test / install
# ----------------------------------------------------------------------------

# Run the meson test suite.
test: build
    meson test -C "{{builddir}}" --print-errorlogs

# Install into the system prefix (needs privileges; usually prefer flatpak).
install: build
    ninja -C "{{builddir}}" install

# ----------------------------------------------------------------------------
# Flatpak
# ----------------------------------------------------------------------------

# Build the flatpak and install it for the current user (builds committed state).
flatpak:
    # NOTE: the manifest sources from git, so this builds your last *committed*
    # state. Commit first if you want your latest working-tree changes.
    flatpak-builder --force-clean --user --install \
        "{{fp-builddir}}" "{{manifest}}"

# Run the installed flatpak.
flatpak-run:
    flatpak run "{{app-id}}"

# Build and export to a local repo, then produce a single-file .flatpak bundle.
flatpak-bundle:
    flatpak-builder --force-clean --repo="{{fp-repo}}" \
        "{{fp-builddir}}" "{{manifest}}"
    flatpak build-bundle "{{fp-repo}}" "{{bundle}}" "{{app-id}}"
    @echo "Wrote {{bundle}} — install with: flatpak install --user {{bundle}}"

# Lint the flatpak manifest (needs the org.flatpak.Builder SDK).
flatpak-lint:
    flatpak run --command=flatpak-builder-lint org.flatpak.Builder manifest "{{manifest}}" \
        || echo "flatpak-builder-lint not installed (install org.flatpak.Builder)"

# ----------------------------------------------------------------------------
# Translations / cleanup
# ----------------------------------------------------------------------------

# Regenerate the .pot translation template.
pot: build
    ninja -C "{{builddir}}" postbox-pot

# Remove the meson build dir.
clean:
    rm -rf "{{builddir}}"

# Remove everything generated (meson + flatpak + bundle).
clean-all:
    rm -rf "{{builddir}}" ".flatpak" "{{bundle}}"
