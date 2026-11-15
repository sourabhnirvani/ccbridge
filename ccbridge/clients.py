"""Where each AI coding tool keeps its MCP servers, and how to add ccbridge there.

One entry per tool in CLIENTS. A wrong path or key is a one-line fix there.

Paths use two placeholders: ``{project}`` (the project folder) and ``~`` (your
home folder). Nearly every tool reads a config from the project, and ccbridge
writes that one: a user-wide entry would start in every project's window, so
two windows would run two servers under one name. A ``|`` separates
candidates: the first that exists is used, else the first is created.

Every config written here holds no secret: only the command that starts
ccbridge's MCP server and CCBRIDGE_CONFIG, the path of a settings file that only
you can read. Nothing here ever pre-approves ccbridge's tools (autoApprove,
alwaysAllow, trust...): its send and share tools must stay behind the tool's
own approval prompt.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

try:  # Python 3.11+: used only to check a TOML file before it is saved
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10
    tomllib = None  # type: ignore[assignment]


class ClientError(Exception):
    """A config file we will not touch, or a tool we cannot set up; printed as is."""


@dataclass(frozen=True)
class Client:
    slug: str
    display: str
    path: str  # "{project}/..." or "~/..."; "a|b" = the first that exists, else a
    style: str  # how the entry looks - see STYLES
    programs: tuple[str, ...] = ()  # found on PATH = installed
    markers: tuple[str, ...] = ()  # home-relative folders whose presence = installed
    only_if_exists: bool = False  # never create this file, only add to it
    source: str = "docs"  # "tested" = seen working here; "docs" = per the tool's docs
    note: str = ""

    @property
    def user_wide(self) -> bool:
        return not self.path.startswith("{project}")


CLIENTS: dict[str, Client] = {c.slug: c for c in [
    Client("claude-code", "Claude Code", "{project}/.mcp.json", "mcpServers",
           programs=("claude",), source="tested"),
    Client("codex", "OpenAI Codex", "{project}/.codex/config.toml", "toml-codex",
           programs=("codex",), markers=(".codex",),
           note="read only in projects Codex trusts"),
    Client("cursor", "Cursor", "{project}/.cursor/mcp.json", "stdio",
           programs=("cursor",), markers=(".cursor",)),
    Client("antigravity", "Google Antigravity", "{project}/.agents/mcp_config.json", "mcpServers",
           programs=("agy", "antigravity"), markers=(".gemini/antigravity", ".gemini/config")),
    Client("grok", "Grok Build", "{project}/.grok/config.toml", "toml",
           programs=("grok",), markers=(".grok",), source="tested"),
    Client("gemini", "Gemini CLI", "{project}/.gemini/settings.json", "mcpServers",
           programs=("gemini",)),
    Client("vscode", "VS Code (GitHub Copilot)", "{project}/.vscode/mcp.json", "vscode",
           programs=("code",)),
    Client("devin", "Devin Desktop (was Windsurf)", "{project}/.devin/mcp_config.json",
           "mcpServers", programs=("devin",), markers=(".config/devin",)),
    Client("windsurf", "Windsurf (older versions)", "~/.codeium/windsurf/mcp_config.json",
           "mcpServers", programs=("windsurf",), only_if_exists=True,
           note="user-wide; only if that file exists - prefer devin"),
    # opencode 1.17 still wants mcp.<name> (with "enabled"), not the newer mcp.servers.<name>.
    Client("opencode", "opencode", "{project}/opencode.jsonc|{project}/opencode.json", "opencode",
           programs=("opencode",), source="tested"),
    Client("kiro", "Kiro", "{project}/.kiro/settings/mcp.json", "mcpServers",
           programs=("kiro",), markers=(".kiro",)),
    Client("amp", "Amp", "{project}/.amp/settings.json", "amp", programs=("amp",)),
    Client("junie", "Junie", "{project}/.junie/mcp/mcp.json", "mcpServers",
           programs=("junie",)),
    Client("zed", "Zed", "{project}/.zed/settings.json", "zed", programs=("zed",)),
]}

ALL_INSTALLED = "all-installed"
DEFAULT_CLIENT = "claude-code"

# The env keys ccbridge manages in a server entry; any other key there is the user's.
MANAGED_ENV = {"CCBRIDGE_CONFIG", "CCBRIDGE_RELAY_URL", "CCBRIDGE_ROOM", "CCBRIDGE_TOKEN",
               "CCBRIDGE_NAME", "CCBRIDGE_IDENTITY_KEY", "CCBRIDGE_ALLOWED_PEERS",
               "CCBRIDGE_CLIENT", "CCBRIDGE_PROJECT_DIR", "PYTHONPATH"}

# style: (key holding the servers, key of the env inside an entry)
STYLES = {"mcpServers": ("mcpServers", "env"), "stdio": ("mcpServers", "env"),
          "vscode": ("servers", "env"), "opencode": ("mcp", "environment"),
          "amp": ("amp.mcpServers", "env"), "zed": ("context_servers", "env")}
# Codex waits 60 s for a tool by default; get_messages may wait up to 25 s itself.
TOML_EXTRA = {"toml": {}, "toml-codex": {"tool_timeout_sec": 120}}


# --- which tools, and where ------------------------------------------------------


def _resolve(template: str, project: Path) -> Path:
    if template.startswith("{project}/"):
        return project / template[len("{project}/"):]
    if template.startswith("~/"):
        return Path.home() / template[2:]
    raise ValueError(f"bad path template: {template}")


def config_path(client: Client, project: Path) -> Path:
    """The file ``client`` reads its MCP servers from, for ``project``. Resolved
    when called, so a changed HOME (tests, say) is picked up."""
    candidates = [_resolve(t, project) for t in client.path.split("|")]
    return next((p for p in candidates if p.exists()), candidates[0])


def is_installed(client: Client, which: Callable[[str], Optional[str]]) -> bool:
    if client.only_if_exists:  # its config file is the sign it is (still) used
        return config_path(client, Path.cwd()).exists()
    if any(which(p) for p in client.programs):
        return True
    return any((Path.home() / m).is_dir() for m in client.markers)


def parse_clients(raw: Optional[str], which: Callable[[str], Optional[str]]) -> list[str]:
    """The slugs named in ``raw`` ("cursor,codex" or "all-installed"), in order."""
    if not raw:
        return [DEFAULT_CLIENT]
    slugs: list[str] = []
    for part in raw.split(","):
        part = part.strip().lower()
        if not part:
            continue
        if part == ALL_INSTALLED:
            found = [s for s, c in CLIENTS.items() if is_installed(c, which)]
            if "devin" in found and "windsurf" in found:
                found.remove("windsurf")  # Devin Desktop is Windsurf's successor
            slugs += found
        elif part in CLIENTS:
            slugs.append(part)
        else:
            known = ", ".join(CLIENTS)
            raise ClientError(f"unknown client {part!r} - use one of: {known}, or {ALL_INSTALLED}")
    slugs = list(dict.fromkeys(slugs))
    if not slugs:
        raise ClientError("none of the supported clients is installed here"
                          " (run `bridge.py clients` to see what was looked for)")
    return slugs


def server_name(client: Client, project_key: str) -> str:
    """The name of ccbridge's entry in ``client``'s config.

    Project configs use "ccbridge". A user-wide config is shared by every
    project, so its entry names the project: ``ccbridge_<slug>``. Only
    [a-z0-9_] with single underscores, and short: some tools drop names with
    '-', '.' or '__', or cut long tool names.
    """
    if not client.user_wide:
        return "ccbridge"
    words = re.sub(r"[^a-z0-9]+", "_", project_key.lower()).strip("_")
    stem, _, digest = words.rpartition("_")
    short = f"{stem[:13].strip('_')}_{digest[:6]}" if stem else digest[:20]
    return "ccbridge_" + re.sub(r"_+", "_", short).strip("_")


def server_entry(style: str, command: str, args: list[str], env: dict) -> dict:
    if style in ("mcpServers", "amp", "zed"):
        return {"command": command, "args": args, "env": env}
    if style in ("stdio", "vscode"):
        return {"type": "stdio", "command": command, "args": args, "env": env}
    if style == "opencode":
        return {"type": "local", "command": [command, *args], "environment": env, "enabled": True}
    raise ValueError(f"no JSON entry for style {style}")


# --- writing files -----------------------------------------------------------------


def _read(path: Path) -> tuple[str, str]:
    """``path``'s text without a byte-order mark, and its line ending; ("", "\\n") if absent."""
    if not path.exists():
        return "", "\n"
    with open(path, encoding="utf-8-sig", newline="") as f:
        text = f.read()
    return text, ("\r\n" if "\r\n" in text else "\n")


def backup(path: Path) -> Optional[Path]:
    """Copy ``path`` aside before changing it; returns the copy's path."""
    if not path.exists():
        return None
    copy = path.with_name(path.name + ".ccbridge-backup")
    shutil.copy2(path, copy)
    return copy


def _save(path: Path, text: str) -> None:
    """Replace ``path`` in one step, keeping its permissions; the tool reading it
    sees the old file or the new one, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.ccbridge-{secrets.token_hex(4)}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    try:
        if path.exists():
            shutil.copymode(path, tmp)
        for attempt in range(100):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:  # Windows: the tool has the file open right now
                if attempt == 99:
                    raise
                time.sleep(0.02)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_json(path: Path, style: str, name: str, command: str, args: list[str],
               env: dict) -> None:
    """Add or replace server ``name`` in a JSON config; everything else is kept."""
    key, env_key = STYLES[style]
    text, eol = _read(path)
    try:
        config = json.loads(text) if text.strip() else {}
    except ValueError as exc:
        # Comments (JSONC) land here too: we cannot rewrite those without losing them.
        raise ClientError(f"{path} is not plain JSON ({exc}); add ccbridge by hand, or fix"
                          " or remove the file") from None
    servers = config.setdefault(key, {}) if isinstance(config, dict) else None
    if not isinstance(servers, dict):
        raise ClientError(f"{path} has an unexpected shape; fix or remove it")
    old = servers.get(name)
    old_env = old.get(env_key) if isinstance(old, dict) else None
    kept = {k: v for k, v in old_env.items() if k not in MANAGED_ENV} if isinstance(old_env, dict) else {}
    servers[name] = server_entry(style, command, args, {**kept, **env})
    _save(path, json.dumps(config, indent=2).replace("\n", eol) + eol)


def _toml_str(value: str) -> str:
    # A literal string ('...') needs no escaping, so Windows paths stay readable.
    if "'" not in value and not any(ord(c) < 32 or ord(c) == 127 for c in value):
        return f"'{value}'"
    return json.dumps(value, ensure_ascii=False)  # a TOML basic string


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return _toml_str(str(value))


def _block_markers(name: str) -> tuple[str, str]:
    return f"# >>> ccbridge {name} >>>", f"# <<< ccbridge {name} <<<"


def toml_block(name: str, command: str, args: list[str], env: dict, eol: str = "\n",
               extra: Optional[dict] = None) -> str:
    start, end = _block_markers(name)
    env_items = ", ".join(f"{_toml_key(k)} = {_toml_str(v)}" for k, v in env.items())
    lines = [
        f"{start} (written by ccbridge's bridge.py; re-running it replaces this block)",
        f"[mcp_servers.{_toml_key(name)}]",
        f"command = {_toml_str(command)}",
        f"args = [{', '.join(_toml_str(a) for a in args)}]",
        f"env = {{ {env_items} }}",
        *(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in (extra or {}).items()),
        end,
    ]
    return eol.join(lines) + eol


def _without_block(text: str, name: str) -> tuple[str, Optional[tuple[int, int]]]:
    start, end = _block_markers(name)
    m = re.search(rf"(?m)^{re.escape(start)}[^\r\n]*\r?\n.*?^{re.escape(end)}[^\r\n]*(?:\r?\n|\Z)",
                  text, re.S)
    if not m:
        return text, None
    return text[:m.start()] + text[m.end():], (m.start(), m.end())


def write_toml(path: Path, name: str, command: str, args: list[str], env: dict,
               extra: Optional[dict] = None) -> None:
    """Add or replace ccbridge's own marked block in a TOML config.

    The file is never parsed and rewritten - only our block changes - so the
    rest of it, comments and all, stays exactly as it was.
    """
    text, eol = _read(path)
    rest, span = _without_block(text, name)
    header = re.compile(
        rf"(?m)^[ \t]*\[[ \t]*mcp_servers[ \t]*\.[ \t]*(?:{re.escape(name)}|\"{re.escape(name)}\"|'{re.escape(name)}')[ \t]*[\].]")
    if header.search(rest):
        raise ClientError(f"{path} already has an [mcp_servers.{name}] table that ccbridge did not"
                          " write; remove it, then run this again")
    block = toml_block(name, command, args, env, eol, extra)
    if span:
        new = text[:span[0]] + block + text[span[1]:]
    else:
        new = rest
        if new and not new.endswith(("\n", "\r")):
            new += eol
        if new.strip():
            new += eol
        new += block
    if tomllib is not None:
        try:
            tomllib.loads(new)
        except tomllib.TOMLDecodeError as exc:
            raise ClientError(f"{path} would not be valid TOML after adding ccbridge ({exc});"
                              " it was left unchanged") from None
    _save(path, new)


def register(client: Client, project: Path, name: str, command: str, args: list[str],
             env: dict) -> tuple[Path, Optional[Path]]:
    """Add ccbridge to ``client``'s config; returns (config path, backup or None).

    A user-wide file is backed up first: it holds the user's other settings.
    """
    path = config_path(client, project)
    if client.only_if_exists and not path.exists():
        raise ClientError(f"{client.display}: {path} does not exist, so this version does not"
                          " use it - pick another client (see `bridge.py clients`)")
    copy = backup(path) if client.user_wide else None
    if client.style in TOML_EXTRA:
        write_toml(path, name, command, args, env, TOML_EXTRA[client.style])
    else:
        write_json(path, client.style, name, command, args, env)
    return path, copy
