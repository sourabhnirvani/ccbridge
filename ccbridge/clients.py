"""Where each AI coding tool keeps its MCP servers, and how to add ccbridge there.

One entry per tool in CLIENTS. A wrong path or key is a one-line fix there.

Paths use two placeholders: ``{project}`` (the project folder, for tools that
read a config from the project) and ``~`` (your home folder, for tools that only
have a user-wide config). ``{codex_home}`` is $CODEX_HOME, or ~/.codex.

Every config written here holds no secret: only the command that starts
ccbridge's MCP server and CCBRIDGE_CONFIG, the path of a settings file that only
you can read.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
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
    path: str  # "{project}/..." for a project config, "~/..." or "{codex_home}/..." for a user one
    style: str  # "mcpServers", "vscode", "opencode" or "toml" - see server_entry()
    programs: tuple[str, ...] = ()  # found on PATH = installed
    markers: tuple[str, ...] = ()  # home-relative folders whose presence = installed
    cli: Optional[str] = None  # a program whose own "mcp add" is used when it is on PATH
    source: str = ""  # how this entry was checked; "docs" = per the tool's docs, not tested here

    @property
    def user_wide(self) -> bool:
        return not self.path.startswith("{project}")


CLIENTS: dict[str, Client] = {c.slug: c for c in [
    Client("claude-code", "Claude Code", "{project}/.mcp.json", "mcpServers",
           programs=("claude",), source="tested"),
    Client("codex", "OpenAI Codex", "{codex_home}/config.toml", "toml",
           programs=("codex",), markers=(".codex",), cli="codex", source="docs"),
    Client("cursor", "Cursor", "{project}/.cursor/mcp.json", "mcpServers",
           programs=("cursor",), markers=(".cursor",), source="docs"),
    Client("antigravity", "Google Antigravity", "~/.gemini/antigravity/mcp_config.json",
           "mcpServers", programs=("antigravity",), markers=(".gemini/antigravity",),
           source="docs"),
    Client("grok", "Grok Build", "{project}/.grok/config.toml", "toml",
           programs=("grok",), markers=(".grok",), source="tested"),
    Client("gemini", "Gemini CLI", "{project}/.gemini/settings.json", "mcpServers",
           programs=("gemini",), source="docs"),
    Client("vscode", "VS Code (GitHub Copilot)", "{project}/.vscode/mcp.json", "vscode",
           programs=("code",), source="docs"),
    Client("windsurf", "Windsurf", "~/.codeium/windsurf/mcp_config.json", "mcpServers",
           programs=("windsurf",), markers=(".codeium/windsurf",), source="docs"),
    Client("opencode", "opencode", "{project}/opencode.json", "opencode",
           programs=("opencode",), source="tested"),
    Client("kiro", "Kiro", "{project}/.kiro/settings/mcp.json", "mcpServers",
           programs=("kiro",), markers=(".kiro",), source="docs"),
]}

ALL_INSTALLED = "all-installed"
DEFAULT_CLIENT = "claude-code"

# The env keys ccbridge manages in a server entry; any other key there is the user's.
MANAGED_ENV = {"CCBRIDGE_CONFIG", "CCBRIDGE_RELAY_URL", "CCBRIDGE_ROOM", "CCBRIDGE_TOKEN",
               "CCBRIDGE_NAME", "CCBRIDGE_IDENTITY_KEY", "CCBRIDGE_ALLOWED_PEERS",
               "CCBRIDGE_CLIENT", "CCBRIDGE_PROJECT_DIR", "PYTHONPATH"}


# --- which tools, and where ------------------------------------------------------


def config_path(client: Client, project: Path) -> Path:
    """The file ``client`` reads its MCP servers from, for ``project``. Resolved
    when called, so a changed HOME or CODEX_HOME (tests, say) is picked up."""
    raw = client.path
    if raw.startswith("{project}"):
        return project / raw[len("{project}/"):]
    if raw.startswith("{codex_home}"):
        base = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        return base / raw[len("{codex_home}/"):]
    if raw.startswith("~/"):
        return Path.home() / raw[2:]
    raise ValueError(f"bad path template for {client.slug}: {raw}")


def is_installed(client: Client, which: Callable[[str], Optional[str]]) -> bool:
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
            slugs += [s for s, c in CLIENTS.items() if is_installed(c, which)]
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

    A user-wide config is shared by every project, so its entry names the
    project: one entry per project, each with its own settings and identity.
    """
    if not client.user_wide:
        return "ccbridge"
    return "ccbridge-" + re.sub(r"[^A-Za-z0-9_-]+", "-", project_key).strip("-")


def server_entry(style: str, command: str, args: list[str], env: dict) -> dict:
    if style == "mcpServers":
        return {"command": command, "args": args, "env": env}
    if style == "vscode":
        return {"type": "stdio", "command": command, "args": args, "env": env}
    if style == "opencode":
        return {"type": "local", "command": [command, *args], "environment": env, "enabled": True}
    raise ValueError(f"no JSON entry for style {style}")


_JSON_KEYS = {"mcpServers": ("mcpServers", "env"), "vscode": ("servers", "env"),
              "opencode": ("mcp", "environment")}


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
    key, env_key = _JSON_KEYS[style]
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


def _block_markers(name: str) -> tuple[str, str]:
    return f"# >>> ccbridge {name} >>>", f"# <<< ccbridge {name} <<<"


def toml_block(name: str, command: str, args: list[str], env: dict, eol: str = "\n") -> str:
    start, end = _block_markers(name)
    env_items = ", ".join(f"{_toml_key(k)} = {_toml_str(v)}" for k, v in env.items())
    lines = [
        f"{start} (written by ccbridge's bridge.py; re-running it replaces this block)",
        f"[mcp_servers.{_toml_key(name)}]",
        f"command = {_toml_str(command)}",
        f"args = [{', '.join(_toml_str(a) for a in args)}]",
        f"env = {{ {env_items} }}",
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


def write_toml(path: Path, name: str, command: str, args: list[str], env: dict) -> None:
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
    block = toml_block(name, command, args, env, eol)
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
             env: dict, which: Callable[[str], Optional[str]]) -> tuple[Path, Optional[Path]]:
    """Add ccbridge to ``client``'s config; returns (config path, backup or None).

    A user-wide file is backed up first: it holds the user's other settings.
    """
    path = config_path(client, project)
    cli = which(client.cli) if client.cli else None
    if cli:
        # The tool's own command knows its file format best.
        env_args = [a for k, v in env.items() for a in ("--env", f"{k}={v}")]
        copy = backup(path)
        subprocess.run([cli, "mcp", "remove", name], capture_output=True, timeout=60)
        done = subprocess.run([cli, "mcp", "add", name, *env_args, "--", command, *args],
                              capture_output=True, text=True, timeout=60)
        if done.returncode != 0:
            raise ClientError(f"`{client.cli} mcp add` failed: {(done.stderr or done.stdout).strip()[:300]}")
        return path, copy
    copy = backup(path) if client.user_wide else None
    if client.style == "toml":
        write_toml(path, name, command, args, env)
    else:
        write_json(path, client.style, name, command, args, env)
    return path, copy
