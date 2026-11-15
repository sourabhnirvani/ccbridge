"""Where the MCP server's settings come from.

``bridge.py host``/``join`` keep the room token and your identity key in a small
settings file inside the ccbridge folder, readable only by you, and .mcp.json
only names that file (``CCBRIDGE_CONFIG``). A project that commits its
.mcp.json - many do, to share MCP servers with a team - then leaks no secret.

Any setting can also be given as an environment variable of the same name,
which wins over the file. That is how a hand-written .mcp.json, or an older
one written by ccbridge 0.2, keeps working.
"""

from __future__ import annotations

import functools
import json
import os
import time
from typing import Optional

CONFIG_ENV = "CCBRIDGE_CONFIG"


class ConfigError(RuntimeError):
    """The settings file named by CCBRIDGE_CONFIG is missing or unreadable."""


@functools.lru_cache(maxsize=8)
def _parse(text: str) -> Optional[dict[str, str]]:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _read(path: str) -> str:
    # bridge.py replaces the file in one step. On Windows, opening it at the very
    # moment it is being replaced fails with PermissionError; a moment later the
    # new file is there.
    for attempt in range(50):
        try:
            with open(path, encoding="utf-8-sig") as f:
                return f.read()
        except PermissionError:
            if attempt == 49:
                raise
            time.sleep(0.01)
    raise AssertionError("unreachable")


def _file_settings() -> dict[str, str]:
    path = os.getenv(CONFIG_ENV, "").strip()
    if not path:
        return {}
    # Read on every call - it is a few hundred bytes - so a re-run of bridge.py
    # takes effect at once; only the parsing is cached, keyed on the content.
    try:
        text = _read(path)
    except FileNotFoundError:
        raise ConfigError(
            f"the ccbridge settings file {path} is missing"
            " - run bridge.py host/join in this project again"
        ) from None
    except (OSError, ValueError) as exc:
        raise ConfigError(
            f"the ccbridge settings file {path} cannot be read ({exc.__class__.__name__})"
            " - run bridge.py host/join in this project again"
        ) from None
    settings = _parse(text)
    if settings is None:
        raise ConfigError(
            f"the ccbridge settings file {path} cannot be read (damaged)"
            " - run bridge.py host/join in this project again"
        )
    return settings


def setting(name: str) -> str:
    """The value of setting ``name``: the environment first, then the settings file.

    Raises ConfigError only when the environment does not have it and the
    settings file is needed but broken.
    """
    value = os.getenv(name, "")
    if value:
        return value
    return _file_settings().get(name, "")


def setting_or_empty(name: str) -> str:
    """Like setting(), but a broken settings file counts as "not set".

    For optional settings: the required ones report the broken file.
    """
    try:
        return setting(name)
    except ConfigError:
        return ""
