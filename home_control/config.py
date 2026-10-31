"""User configuration: a TOML file read once at startup.

Location resolution: ``$HOME_CONTROL_CONFIG`` → ``~/.config/home-control/config.toml``.
The file is auto-created with a commented template on first run. Parsing is
read-only (tomllib); to keep user comments intact we never rewrite it — empty
values just fall back to sensible defaults / auto-discovery.
"""

from __future__ import annotations

import os
import stat
import threading
import tomllib
import warnings
from importlib.resources import files
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(
    os.environ.get("HOME_CONTROL_CONFIG", Path.home() / ".config" / "home-control" / "config.toml")
)

TEMPLATE_NAME = "config.example.toml"


def template_text() -> str:
    """The commented template a missing config is created from."""
    return files(__package__).joinpath(TEMPLATE_NAME).read_text(encoding="utf-8")

_lock = threading.Lock()
_cache: dict[str, Any] | None = None
PRIVATE_FILE_MODE = 0o600


def prepare_private_file(path: Path) -> None:
    """Create ``path`` privately, or warn if an existing file is exposed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, PRIVATE_FILE_MODE)
    try:
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
    finally:
        os.close(fd)
    _warn_if_exposed(path, mode)


def check_private_file(path: Path) -> None:
    """Warn if an existing ``path`` is exposed; a missing one stays missing."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return
    _warn_if_exposed(path, mode)


def _warn_if_exposed(path: Path, mode: int) -> None:
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        warnings.warn(
            f"{path} has insecure permissions {mode:04o}; expected no group or other access",
            RuntimeWarning,
        )


def write_private_text(path: Path, text: str) -> None:
    """Write text, creating a missing file with mode no broader than 0600."""
    prepare_private_file(path)
    flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1
            f.write(text)
    finally:
        if fd >= 0:
            os.close(fd)


def _load() -> dict[str, Any]:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        if not CONFIG_PATH.exists():
            try:
                write_private_text(CONFIG_PATH, template_text())
            except OSError:
                pass
        else:
            try:
                prepare_private_file(CONFIG_PATH)
            except OSError:
                pass
        data: dict[str, Any] = {}
        try:
            with open(CONFIG_PATH, "rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        _cache = data
        return data


def section(name: str) -> dict[str, Any]:
    """Return a config table (e.g. ``[hue]``), or an empty dict if absent."""
    return _load().get(name, {}) or {}


def get(sect: str, key: str, default: Any = None) -> Any:
    """Return a single config value, treating empty strings/lists as unset."""
    val = section(sect).get(key, default)
    if val == "" or val == []:
        return default
    return val
