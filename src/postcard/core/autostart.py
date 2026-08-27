from pathlib import Path

from gi.repository import GLib

APP_ID = "in.gxanshu.postcard"
ENTRY_NAME = f"{APP_ID}.desktop"

IS_SANDBOXED = Path("/.flatpak-info").exists()


def user_directory() -> Path:
    # Inside the sandbox XDG_CONFIG_HOME points at the app's private config,
    # but the host only ever reads ~/.config/autostart.
    if IS_SANDBOXED:
        return Path.home() / ".config" / "autostart"
    return Path(GLib.get_user_config_dir()) / "autostart"


def set_entry(directory: Path, *, is_enabled: bool) -> None:
    """Write or remove the autostart entry, raising OSError if that fails."""
    entry = directory / ENTRY_NAME
    if not is_enabled:
        entry.unlink(missing_ok=True)
        return

    # The host launches this, so it cannot rely on anything on the sandbox PATH.
    command = f"flatpak run {APP_ID}" if IS_SANDBOXED else "postcard"
    directory.mkdir(parents=True, exist_ok=True)
    entry.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Postcard\n"
        f"Exec={command} --hidden\n"
        f"Icon={APP_ID}\n"
    )
