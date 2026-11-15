"""One-command setup for ccbridge. Run it from the project folder you will open
Claude Code in - it writes .mcp.json there.

    python <ccbridge>/bridge.py host <yourname>           # you: starts everything, prints an invite
    python <ccbridge>/bridge.py join <link> <yourname>    # your friend: pastes the join link, done

host prints a join link (https://<bridge>/j/XXXX-XXXX) and the older, longer
invite string; join takes either.

Add --allow <name> to either to accept messages from that peer only. On host it
also locks the room: nobody else can join, even with the invite. Add --new-room
to host to start over with a fresh room and token, for example after an invite
leaked.

Other AI coding tools (Cursor, Codex, Grok, VS Code, ...) can be on the bridge
too, each under a name of its own - see docs/CLIENTS.md:

    python <ccbridge>/bridge.py clients                     # supported tools, and which are installed
    python <ccbridge>/bridge.py host <yourname> --client claude-code,cursor
    python <ccbridge>/bridge.py connect --client codex,grok # later, in a project that ran host/join

Add --local to host for tools on this computer only (no tunnel).

Everything either side needs - relay URL, room, token - is packed into the single
invite string, so nobody has to copy three separate values into a config file.
The token and your identity key are kept in a settings file inside the ccbridge
folder that only you can read; .mcp.json only names that file, so it is safe
even in a project that commits its .mcp.json.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import httpx

from ccbridge.clients import (CLIENTS, DEFAULT_CLIENT, MANAGED_ENV, ClientError, config_path,
                              is_installed, parse_clients, register, server_name)
from ccbridge.identity import LEGACY_CLAIM_HEADER, join_body, legacy_key_hash
from ccbridge.net import http_client
from ccbridge.safety import NAME_RE, ROOM_RE

HERE = Path(__file__).resolve().parent
PORT = int(os.getenv("CCBRIDGE_PORT", "8787"))
# A quick tunnel's address is a few random words: https://word-word-word.trycloudflare.com.
# The (?!api\.) is belt and braces: when cloudflared cannot get a tunnel it logs
# the API it called, https://api.trycloudflare.com, which is no relay.
TUNNEL_URL_RE = re.compile(rb"https://(?!api\.)[a-z0-9]+(?:-[a-z0-9]+)+\.trycloudflare\.com")
TUNNEL_WAIT_SECONDS = 30.0
PUBLIC_CHECK_SECONDS = 20.0
RELAY_START_SECONDS = 15.0

# All of these hold secrets, live next to this script, and are gitignored there.
IDENTITY_FILE = HERE / ".ccbridge_identity"
PROJECTS_DIR = HERE / ".ccbridge_projects"  # one settings file per project folder
INVITE_FILE = HERE / "invite.txt"
DB_FILE = HERE / "ccbridge.db"
# Where ccbridge 0.2 kept the one room this whole folder hosted.
LEGACY_TOKEN_FILE = HERE / ".ccbridge_token"

# What .mcp.json starts. Run as a script by its absolute path, so Python looks
# for modules next to it - never in the project folder Claude Code starts it
# from, where a folder named `mcp` or `ccbridge` would shadow the real packages.
LAUNCHER = HERE / "run_mcp_server.py"

# Keys of the 'ccbridge' entry's env in .mcp.json that this script manages. Any
# other key there (CCBRIDGE_CONFIRM_SENDS, say) is the user's and is kept.
_MANAGED_ENV = MANAGED_ENV


class SetupError(Exception):
    """A problem the person running this can fix; printed without a traceback."""


class RelayRefused(Exception):
    """The relay answered a join with an error."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class RelayUnreachable(Exception):
    """No relay answered at that address: what happened instead is the message."""


def encode_invite(url: str, room: str, token: str) -> str:
    blob = json.dumps({"u": url, "r": room, "t": token}, separators=(",", ":"))
    return base64.urlsafe_b64encode(blob.encode()).decode().rstrip("=")


def decode_invite(invite: str) -> tuple[str, str, str]:
    invite = invite.strip()
    try:
        data = json.loads(base64.urlsafe_b64decode(invite + "=" * (-len(invite) % 4)))
        url, room, token = data["u"], data["r"], data["t"]
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise SetupError("that invite is not valid - copy the whole invite again") from None
    if not (isinstance(url, str) and url.startswith(("http://", "https://"))
            and isinstance(room, str) and ROOM_RE.match(room)
            and isinstance(token, str) and token):
        raise SetupError("that invite is not valid - copy the whole invite again")
    return url, room, token


def check_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise SetupError(
            f"{name!r} is not a usable name - use up to 64 letters, digits, spaces, '_', '.' or '-'"
        )
    return name


# --- files -------------------------------------------------------------------


def _make_private(path: Path) -> None:
    """Make ``path`` readable by you only (0600; macOS and Linux - Windows has no such mode)."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _write_private(path: Path, text: str) -> None:
    """Write a file only you can read, in one step.

    The text goes into a new 0600 file that then replaces ``path``, so it is never
    readable by others, and a reader - the MCP server, re-reading its settings -
    sees the old file or the new one, never half of one.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".ccbridge-{path.name}-{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        for attempt in range(100):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows refuses while another program has the file open - the
                # MCP server reading its settings, for a moment.
                if attempt == 99:
                    raise
                time.sleep(0.02)
    except BaseException:
        _remove(tmp)
        raise


def find_program(name: str) -> Optional[str]:
    """``name`` from a folder on PATH - never from the current folder.

    On Windows, shutil.which looks in the current folder first, so a git.cmd or
    cloudflared.exe in the project would run in place of the real one.
    """
    if os.name == "nt":
        exts = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if e]
    else:
        exts = [""]
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        folder = folder.strip().strip('"')
        if not folder or not os.path.isabs(folder):
            continue
        for ext in exts:
            candidate = os.path.join(folder, name + ext)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _read_or_none(path: Path) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()
    except (OSError, ValueError):
        return None


def _read_text(path: Path) -> tuple[str, str]:
    """``path``'s text, minus any byte-order mark, and the line ending it uses."""
    # utf-8-sig: Notepad and PowerShell 5 save UTF-8 with a BOM, which json rejects.
    with open(path, encoding="utf-8-sig", newline="") as f:
        text = f.read()
    return text, ("\r\n" if "\r\n" in text else "\n")


def identity_key() -> str:
    """This machine's identity key, created on first use.

    It owns your display name in every room you join from this ccbridge folder,
    so rejoining with a fresh invite keeps your name. Keep it private.
    """
    if IDENTITY_FILE.exists():
        key = IDENTITY_FILE.read_text(encoding="utf-8-sig").strip()
        if len(key) >= 16:
            _make_private(IDENTITY_FILE)  # versions before 0.3 left it readable by all
            return key
    key = secrets.token_urlsafe(32)
    _write_private(IDENTITY_FILE, key + "\n")
    return key


def settings_path(project: Path) -> Path:
    """The settings file for ``project``: one per project folder, inside PROJECTS_DIR."""
    resolved = project.resolve()
    digest = hashlib.sha256(
        os.path.normcase(str(resolved)).encode("utf-8", "surrogatepass")).hexdigest()[:12]
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", resolved.name).strip("-.")[:40] or "project"
    return PROJECTS_DIR / f"{slug}-{digest}.json"


def client_settings_path(project: Path, client: str) -> Path:
    """The settings file ``client`` uses in ``project``.

    Claude Code keeps the project's own file. Every other tool gets a file of
    its own, with a name of its own: two tools sharing one would join as the same
    member and take each other's messages.
    """
    base = settings_path(project)
    return base if client == DEFAULT_CLIENT else base.with_name(f"{base.stem}-{client}.json")


def client_name(base: str, client: str) -> str:
    """The name ``client`` appears under: yours for Claude Code, else ``<yours>-<client>``."""
    return base if client == DEFAULT_CLIENT else f"{base}-{client}"


def _client_names(base: str, clients: list[str], override: Optional[str] = None) -> dict[str, str]:
    if override and len(clients) == 1:
        names = {clients[0]: override}
    else:
        names = {c: client_name(override or base, c) for c in clients}
    for name in names.values():
        if not NAME_RE.match(name):
            raise SetupError(f"{name!r} is not a usable name - names are up to 64 letters, digits,"
                             " spaces, '_', '.' or '-'; pick a shorter one with --name")
    return names


def _moved_settings(project: Path) -> Optional[Path]:
    """The settings file of the folder this project was moved or renamed from, if any.

    Found through the path in its .mcp.json. Only if that folder is gone: a copy
    of a project (the original still there) gets a room of its own.
    """
    try:
        config, _ = _load_mcp_config(project / ".mcp.json")
    except SetupError:
        return None
    entry = config["mcpServers"].get("ccbridge")
    env = entry.get("env") if isinstance(entry, dict) else None
    named = env.get("CCBRIDGE_CONFIG") if isinstance(env, dict) else None
    if not isinstance(named, str):
        return None
    path = Path(named)
    try:
        if path.resolve().parent != PROJECTS_DIR.resolve() or not path.is_file():
            return None
        old = json.loads(_read_text(path)[0]).get("project")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(old, str) or Path(old).exists():
        return None
    return path


def _settings_file(project: Path) -> Path:
    """Where ``project``'s settings are now: its own file, or the one it was moved from."""
    path = settings_path(project)
    if path.exists():
        return path
    return _moved_settings(project) or path


def load_settings(project: Path) -> dict:
    path = _settings_file(project)
    if not path.exists():
        return {}
    try:
        data = json.loads(_read_text(path)[0])
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        raise SetupError(f"{path} is damaged - delete it and run this again")
    return data


def _load_mcp_config(path: Path) -> tuple[dict, str]:
    """The project's .mcp.json (empty if there is none) and its line ending."""
    if not path.exists():
        return {"mcpServers": {}}, "\n"
    try:
        text, eol = _read_text(path)
        config = json.loads(text) if text.strip() else {}
    except (OSError, ValueError) as exc:
        raise SetupError(f"{path} exists but is not valid JSON ({exc}); fix or remove it") from None
    servers = config.setdefault("mcpServers", {}) if isinstance(config, dict) else None
    if not isinstance(servers, dict):
        raise SetupError(f"{path} has an unexpected shape; fix or remove it")
    return config, eol


def _ensure_gitignored(entry: str) -> None:
    ignore = Path.cwd() / ".gitignore"
    existing = ""
    if ignore.exists():
        # newline="": keep the file's own line endings (an LF file stays LF on
        # Windows). surrogateescape: never choke on, or mangle, non-UTF-8 bytes.
        with open(ignore, encoding="utf-8", errors="surrogateescape", newline="") as f:
            existing = f.read()
    lines = {line.strip().lstrip("﻿") for line in existing.splitlines()}
    if entry in lines or f"/{entry}" in lines:
        return
    eol = "\r\n" if "\r\n" in existing else "\n"
    if existing and not existing.endswith(("\n", "\r")):
        existing += eol
    with open(ignore, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(existing + entry + eol)


def write_mcp_config(url: str, room: str, token: str, name: str, peer: Optional[str],
                     key: Optional[str] = None, *, hosted: Optional[dict] = None,
                     write_file: bool = True) -> Path:
    """Point Claude Code in the current folder at the bridge; returns .mcp.json's path.

    The token and identity key go into this project's settings file, which only
    you can read; .mcp.json gets nothing but that file's path. ``hosted``
    records the room this project hosts, so the next ``host`` here reuses it.

    That settings file is also the project's record of its bridge, which
    ``connect`` copies for other tools. With ``write_file`` False only it is
    written, and .mcp.json is left alone (Claude Code was not asked for).
    """
    project = Path.cwd()
    path = project / ".mcp.json"
    config, eol = _load_mcp_config(path)  # refuse a broken file before writing anything

    try:
        previous = load_settings(project)
    except SetupError:
        previous = {}  # damaged: rewritten below
    settings: dict = {
        "CCBRIDGE_RELAY_URL": url,
        "CCBRIDGE_ROOM": room,
        "CCBRIDGE_TOKEN": token,
        "CCBRIDGE_NAME": name,
        "CCBRIDGE_IDENTITY_KEY": key or identity_key(),
        "CCBRIDGE_CLIENT": DEFAULT_CLIENT,
        "CCBRIDGE_PROJECT_DIR": str(project.resolve()),
        "project": str(project.resolve()),
    }
    if peer:
        settings["CCBRIDGE_ALLOWED_PEERS"] = peer
    hosted = hosted or previous.get("hosted")
    if isinstance(hosted, dict):
        settings["hosted"] = hosted
    settings_file = settings_path(project)
    moved_from = _settings_file(project)
    _write_private(settings_file, json.dumps(settings, indent=2) + "\n")
    if not write_file:
        if moved_from != settings_file:
            _remove(moved_from)
        return path

    servers = config["mcpServers"]
    old = servers.get("ccbridge")
    old_env = old.get("env") if isinstance(old, dict) else None
    env = {k: v for k, v in old_env.items() if k not in _MANAGED_ENV} if isinstance(old_env, dict) else {}
    env["CCBRIDGE_CONFIG"] = str(settings_file)
    servers["ccbridge"] = {"command": sys.executable, "args": [str(LAUNCHER)], "env": env}
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(json.dumps(config, indent=2).replace("\n", eol) + eol)
    if moved_from != settings_file:
        _remove(moved_from)  # the project moved: its settings (and room) moved with it

    # No secret in it any more, but its ccbridge entry only works on this machine.
    _ensure_gitignored(".mcp.json")
    return path


def write_client(project: Path, client: str, name: str, base: dict) -> tuple[Path, Optional[Path]]:
    """Point ``client`` in ``project`` at the bridge in ``base`` (the project's
    settings), as ``name``. Returns the config file written, and its backup if
    one was made. Claude Code's is .mcp.json, written by write_mcp_config."""
    if client == DEFAULT_CLIENT:
        path = write_mcp_config(base["CCBRIDGE_RELAY_URL"], base["CCBRIDGE_ROOM"],
                                base["CCBRIDGE_TOKEN"], name, base.get("CCBRIDGE_ALLOWED_PEERS"),
                                key=base.get("CCBRIDGE_IDENTITY_KEY"))
        return path, None
    spec = CLIENTS[client]
    resolved = str(project.resolve())
    settings = {k: v for k, v in base.items() if k.startswith("CCBRIDGE_")}
    settings.update(CCBRIDGE_NAME=name, CCBRIDGE_CLIENT=client,
                    CCBRIDGE_PROJECT_DIR=resolved, project=resolved)
    settings_file = client_settings_path(project, client)
    _write_private(settings_file, json.dumps(settings, indent=2) + "\n")
    entry = server_name(spec, settings_path(project).stem)
    try:
        path, copy = register(spec, project, entry, sys.executable, [str(LAUNCHER)],
                              {"CCBRIDGE_CONFIG": str(settings_file)})
    except ClientError as exc:
        raise SetupError(str(exc)) from None
    if not spec.user_wide:
        _ensure_gitignored(path.relative_to(project).as_posix())
    return path, copy


def _sync_allowed_peers(project: Path) -> None:
    """With an allowlist (--allow), let this project's tools hear each other too.

    Every tool's settings start from the same CCBRIDGE_ALLOWED_PEERS - "alex",
    say - which would make sourabh-cursor drop messages from sourabh. So each
    file's list gets the names of this project's other tools. Run after every
    host, join and connect, so a tool connected later is added to the earlier
    tools' files as well. (Running MCP servers re-read their settings.)
    """
    files = {c: client_settings_path(project, c) for c in CLIENTS}
    loaded = {}
    for client, path in files.items():
        try:
            data = json.loads(_read_text(path)[0]) if path.exists() else None
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("CCBRIDGE_NAME"), str):
            loaded[client] = data
    own = {data["CCBRIDGE_NAME"] for data in loaded.values()}
    for client, data in loaded.items():
        allowed = data.get("CCBRIDGE_ALLOWED_PEERS")
        if not isinstance(allowed, str) or not allowed.strip():
            continue
        names = [n.strip() for n in allowed.split(",") if n.strip()]
        merged = list(dict.fromkeys(names + sorted(own - {data["CCBRIDGE_NAME"]})))
        if merged != names:
            data["CCBRIDGE_ALLOWED_PEERS"] = ",".join(merged)
            _write_private(files[client], json.dumps(data, indent=2) + "\n")


def _claim_names(url: str, room: str, token: str, names: dict[str, str], key: str) -> Optional[str]:
    """Claim every name in ``names`` (client -> name). Returns why the relay could
    not be reached, if it could not; raises SetupError if it said no."""
    for client, name in names.items():
        try:
            register_name(url, room, token, name, key, client=client)
        except RelayUnreachable as exc:
            return str(exc)
        except RelayRefused as exc:
            if exc.status == 403 and "only lets in" in exc.detail:
                raise SetupError(
                    f"the room is locked, and '{name}' is not one of the names its host let in."
                    f" Ask the host to restart with --client {client} (or --allow {name}).") from None
            raise _join_refused(exc) from None
    return None


def _print_restart(clients: list[str], paths: dict[str, Path]) -> None:
    for client in clients:
        spec = CLIENTS[client]
        print(f"  {spec.display:<26} {paths[client]}")
    print("\nNow restart each of those in this folder and approve its ccbridge MCP server.")


def _git(*args: str) -> Optional[subprocess.CompletedProcess]:
    git = find_program("git")
    if not git:
        return None
    try:
        return subprocess.run([git, *args], cwd=Path.cwd(), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None


def mcp_config_warnings() -> list[str]:
    """What to tell the user about git and this project's .mcp.json."""
    notes = []
    tracked = _git("ls-files", "--error-unmatch", "--", ".mcp.json")
    if tracked is not None and tracked.returncode == 0:
        notes.append(
            ".mcp.json is tracked by git in this project. It holds no secret, but its"
            " 'ccbridge' entry points at files on this machine - leave that change out"
            " of your commits."
        )
    # Versions before 0.3 kept the token in .mcp.json itself.
    leaked = _git("log", "--all", "--format=%h", "-S", "CCBRIDGE_TOKEN", "--", ".mcp.json")
    if leaked is not None and leaked.returncode == 0 and leaked.stdout.strip():
        notes.append(
            f"git history has an .mcp.json with a ccbridge room token in it (commit"
            f" {leaked.stdout.split()[0]}). Anyone with a copy of this repository can"
            " join that room: whoever hosts it should run `bridge.py host --new-room`."
        )
    return notes


def client_config_warnings(project: Path, paths: dict[str, Path]) -> list[str]:
    """Notes for tool configs git already tracks here (.gitignore does not cover those)."""
    notes = []
    for client, path in paths.items():
        if client == DEFAULT_CLIENT or CLIENTS[client].user_wide:
            continue  # .mcp.json: see mcp_config_warnings
        rel = path.relative_to(project).as_posix()
        tracked = _git("ls-files", "--error-unmatch", "--", rel)
        if tracked is not None and tracked.returncode == 0:
            notes.append(f"{rel} is tracked by git in this project. Its 'ccbridge' entry points"
                         " at files on this machine - leave that change out of your commits.")
    return notes


def _warn_if_inside_ccbridge() -> None:
    if Path.cwd().resolve() == HERE:
        print("Note: you ran this inside the ccbridge folder, so the bridge only loads when")
        print("      Claude Code is opened here. To use it in your project, run this from")
        print("      the project folder instead.\n")


def _print_notes(notes: list[str]) -> None:
    for note in notes:
        print(f"Note: {note}\n")


# --- talking to a relay --------------------------------------------------------


def _relay_detail(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
    except Exception:  # noqa: BLE001 - not JSON, or not an object
        detail = None
    text = detail if isinstance(detail, str) else resp.text
    # The relay may be someone else's: print nothing that could drive the terminal.
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return re.sub(r"\s+", " ", text).strip()[:200]


def _from_the_relay(resp: httpx.Response) -> bool:
    """True if this error is the relay's own answer, not something in front of it.

    A quick tunnel whose bridge has stopped still answers - Cloudflare sends an
    HTML error page (530) - and so does any proxy or gateway. Only the relay
    answers a 4xx with a JSON detail.
    """
    if not 400 <= resp.status_code < 500 or resp.status_code == 404:
        return False
    try:
        data = resp.json()
    except ValueError:
        return False
    return isinstance(data, dict) and "detail" in data


def _register_body(room: str, name: str, key: str, client: Optional[str]) -> dict:
    import inspect

    # Relays that know client kinds show peers which tool each member is.
    if client and "client" in inspect.signature(join_body).parameters:
        return join_body(room, name, key, register_only=True, client=client)
    return join_body(room, name, key, register_only=True)


def register_name(url: str, room: str, token: str, name: str, key: str,
                  client: Optional[str] = None) -> None:
    """Claim ``name`` in ``room`` for this machine's identity key, without going online.

    Raises RelayRefused if the relay says no, RelayUnreachable if no relay answers.
    """
    url = url.rstrip("/")
    body = _register_body(room, name, key, client)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with http_client(timeout=15.0) as http:
            resp = http.post(f"{url}/v1/join", json=body, headers=headers)
            if resp.status_code == 409 and resp.headers.get(LEGACY_CLAIM_HEADER) == "1":
                # Our own name from ccbridge 0.2: prove it with the hash 0.2 stored.
                body["legacy_key_hash"] = legacy_key_hash(key)
                resp = http.post(f"{url}/v1/join", json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise RelayUnreachable(type(exc).__name__) from None
    if resp.status_code == 200:
        return
    if not _from_the_relay(resp):
        raise RelayUnreachable(f"HTTP {resp.status_code}")
    raise RelayRefused(resp.status_code, _relay_detail(resp))


def check_public_url(url: str, wait: float = PUBLIC_CHECK_SECONDS) -> Optional[str]:
    """None once ``url`` reaches this relay; otherwise what went wrong last."""
    deadline = time.monotonic() + wait
    problem = "no answer"
    with http_client(timeout=5.0) as http:
        while True:
            try:
                resp = http.get(f"{url}/healthz")
                data = resp.json() if resp.status_code == 200 else None
                if isinstance(data, dict) and data.get("ok") is True:
                    return None
                problem = f"HTTP {resp.status_code}"
            except (httpx.HTTPError, ValueError) as exc:
                problem = type(exc).__name__
            if time.monotonic() >= deadline:
                return problem
            time.sleep(1.0)


# --- running the relay and the tunnel ------------------------------------------


def _bind(port: int) -> socket.socket:
    """Take the relay's port before anything else starts, or say who has it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name != "nt":
        # Lets a restarted host reuse the port at once. (On Windows the same
        # option would let a second program bind the port alongside us.)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
        # Listening now is what holds the port: on Linux, a socket that is only
        # bound does not stop a second one from binding it too.
        sock.listen(128)
    except OSError:
        sock.close()
        raise SetupError(
            f"port {port} is already in use - is a bridge already running? Stop it first,"
            " or set CCBRIDGE_PORT to another port"
        ) from None
    return sock


def _prepare_database() -> None:
    """The relay's database holds message text, so make it readable by you only."""
    if not DB_FILE.exists():
        # SQLite takes an empty file as an empty database, and gives its -wal
        # and -shm files the same mode.
        os.close(os.open(DB_FILE, os.O_WRONLY | os.O_CREAT, 0o600))
    for path in (DB_FILE, DB_FILE.with_name(DB_FILE.name + "-wal"),
                 DB_FILE.with_name(DB_FILE.name + "-shm")):
        if path.exists():
            _make_private(path)


def _relay_accepts(option: str) -> bool:
    """Whether this checkout's relay takes ``option`` (newer relays take more)."""
    import inspect

    from ccbridge.relay import create_app

    return option in inspect.signature(create_app).parameters


def build_relay(room: str, token: str, names: Optional[list[str]],
                max_members: Optional[int] = None, invite_code: Optional[str] = None):
    """The relay app for one room. Its settings are passed in, never put in the
    environment, which cloudflared and git would inherit - token included."""
    import inspect

    from ccbridge.relay import create_app

    extra = {}
    if max_members is not None:
        if "max_members" not in inspect.signature(create_app).parameters:
            raise SetupError("this version of the relay has no member limit yet;"
                             " run host without --max-members")
        extra["max_members"] = max_members
    if invite_code:
        extra["invite_codes"] = {invite_code: (room, token)}
    _prepare_database()
    try:
        return create_app(str(DB_FILE), open_rooms=False, rooms={room: token},
                          allowed_names=names or [], **extra)
    except sqlite3.Error as exc:
        raise SetupError(f"cannot open the relay's database {DB_FILE} ({exc})") from None


def start_relay(sock: socket.socket, app):
    """Serve ``app`` on ``sock`` from a background thread; return once it is up."""
    import uvicorn

    config = uvicorn.Config(app, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]},
                              daemon=True, name="ccbridge-relay")
    thread.start()
    deadline = time.monotonic() + RELAY_START_SECONDS
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            server.should_exit = True
            raise SetupError("the relay did not start (see the error above)")
        time.sleep(0.05)
    return server, thread


def stop_relay(server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=10)


def serve_until_stopped(thread: threading.Thread) -> None:
    """Block until Ctrl+C, or until the relay stops by itself."""
    try:
        while thread.is_alive():
            thread.join(0.5)  # short waits, so Ctrl+C is noticed on Windows too
        print("\nThe relay stopped.")
    except KeyboardInterrupt:
        print("\nStopping the bridge...")


@contextlib.contextmanager
def _stop_on_signals():
    """Treat the usual ways of stopping a background process like Ctrl+C, so the
    cleanup (removing the invite, stopping cloudflared) runs for them too.

    A hard kill (SIGKILL, or TerminateProcess on Windows) cannot be caught: then
    invite.txt stays until the next run, which removes it first thing.
    """
    def interrupt(signum, frame):
        raise KeyboardInterrupt

    names = ("SIGTERM", "SIGHUP", "SIGBREAK")  # SIGBREAK: Ctrl+Break on Windows
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for name in names:
            if hasattr(signal, name):
                try:
                    previous[name] = signal.signal(getattr(signal, name), interrupt)
                except (OSError, ValueError):
                    pass
    try:
        yield
    finally:
        for name, handler in previous.items():
            signal.signal(getattr(signal, name), handler)


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def tunnel_url_in(line: bytes) -> Optional[str]:
    """The public URL, if this line of cloudflared output announces the tunnel."""
    match = TUNNEL_URL_RE.search(line)
    return match.group(0).decode() if match else None


def start_tunnel(port: int = PORT) -> tuple[Optional[subprocess.Popen], Optional[str]]:
    """Start cloudflared and return ``(process, public URL)``.

    ``(None, None)`` means cloudflared is not installed. ``(process, None)`` means
    it is installed but produced no URL in time; the process is already stopped.
    """
    exe = find_program("cloudflared")
    if not exe:
        return None, None
    proc = subprocess.Popen(
        # 127.0.0.1, the address the relay holds - not "localhost", which may
        # resolve to ::1, where another program could be listening on this port.
        [exe, "tunnel", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    url: list[str] = []

    def read() -> None:
        # Keep draining after the URL shows up, or cloudflared blocks on a full pipe.
        for line in proc.stdout:  # type: ignore[union-attr]
            found = tunnel_url_in(line)
            if found and not url:
                url.append(found)

    threading.Thread(target=read, daemon=True).start()
    deadline = time.monotonic() + TUNNEL_WAIT_SECONDS
    while time.monotonic() < deadline and not url and proc.poll() is None:
        time.sleep(0.1)
    if url:
        return proc, url[0]
    _stop(proc)
    return proc, None


# --- host ----------------------------------------------------------------------


def _current_bridge_room() -> Optional[str]:
    """The room in this project's .mcp.json, as versions before 0.3 wrote it."""
    try:
        config, _ = _load_mcp_config(Path.cwd() / ".mcp.json")
    except SetupError:
        return None
    entry = config["mcpServers"].get("ccbridge")
    env = entry.get("env") if isinstance(entry, dict) else None
    room = env.get("CCBRIDGE_ROOM") if isinstance(env, dict) else None
    return room if isinstance(room, str) else None


def _legacy_room() -> Optional[tuple[str, str]]:
    """The room ccbridge 0.2 hosted from this folder, if this project used it.

    0.2 kept one room for the whole ccbridge folder. The first project to host
    whose .mcp.json still uses that room takes it over (and the 0.2 file is then
    retired), so a friend's name and unread messages survive the upgrade; every
    other project gets a room of its own.
    """
    if not LEGACY_TOKEN_FILE.exists():
        return None
    _make_private(LEGACY_TOKEN_FILE)
    try:
        parts = LEGACY_TOKEN_FILE.read_text(encoding="utf-8-sig").split()
    except (OSError, ValueError):
        return None
    if len(parts) < 2 or not ROOM_RE.match(parts[0]) or _current_bridge_room() != parts[0]:
        return None
    return parts[0], parts[1]


def host_room(project: Path, new_room: bool) -> tuple[str, str, bool]:
    """The room and token ``project`` hosts: the same on every run, unless ``new_room``.

    The third value is True when that is 0.2's room, which the caller retires
    once it is recorded as this project's.
    """
    legacy = None
    if not new_room:
        hosted = load_settings(project).get("hosted")
        if isinstance(hosted, dict):
            room, token = hosted.get("room"), hosted.get("token")
            if (isinstance(room, str) and ROOM_RE.match(room)
                    and isinstance(token, str) and len(token) >= 16):
                return room, token, False
        legacy = _legacy_room()
        if legacy:
            return legacy[0], legacy[1], True
    elif _legacy_room():
        # --new-room in the project that used 0.2's room: that room is the one
        # being replaced, so no other project may take it over later.
        _remove(LEGACY_TOKEN_FILE)
    return f"bridge-{secrets.token_hex(4)}", secrets.token_urlsafe(32), False


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
INVITE_CODE_RE = re.compile(r"^[0-9A-Za-z]{4}-?[0-9A-Za-z]{4}\Z")
# A join link: <relay>/j/<code>, with or without a trailing slash.
JOIN_LINK_RE = re.compile(r"^(https?://[^\s/?#]+(?:/[^\s?#]*?)?)/j/([0-9A-Za-z]{4}-?[0-9A-Za-z]{4})/?\Z")


def new_invite_code() -> str:
    """A fresh join-link code, "XXXX-XXXX" (40 random bits, Crockford base32)."""
    try:
        from ccbridge.relay import make_invite_code
    except ImportError:
        n = secrets.randbits(40)
        text = "".join(_CROCKFORD[(n >> (5 * i)) & 31] for i in reversed(range(8)))
        return f"{text[:4]}-{text[4:]}"
    return make_invite_code()


def _hosted_code(project: Path, room: str) -> str:
    """This project's join-link code for ``room``: the one it had, or a new one."""
    hosted = load_settings(project).get("hosted")
    if isinstance(hosted, dict) and hosted.get("room") == room:
        code = hosted.get("code")
        if isinstance(code, str) and INVITE_CODE_RE.match(code):
            return code
    return new_invite_code()


def parse_join_link(text: str) -> Optional[tuple[str, str]]:
    """``(relay URL, code)`` if ``text`` is a join link, else None."""
    match = JOIN_LINK_RE.match(text.strip())
    return (match.group(1), match.group(2)) if match else None


def redeem_join_link(base: str, code: str) -> tuple[str, str]:
    """The (room, token) a join link stands for, asked of the relay that made it.

    Raises SetupError if the relay says no, RelayUnreachable if no relay answers.
    """
    try:
        with http_client(timeout=15.0) as http:
            resp = http.post(f"{base}/v1/invite", json={"code": code})
    except httpx.HTTPError as exc:
        raise RelayUnreachable(type(exc).__name__) from None
    if resp.status_code == 200:
        try:
            data = resp.json()
            room, token = data["room"], data["token"]
        except (ValueError, KeyError, TypeError):
            raise RelayUnreachable("the answer was not from a ccbridge relay") from None
        if not (isinstance(room, str) and ROOM_RE.match(room) and isinstance(token, str) and token):
            raise RelayUnreachable("the answer was not from a ccbridge relay")
        return room, token
    try:
        has_detail = isinstance(resp.json(), dict) and "detail" in resp.json()
    except ValueError:
        has_detail = False
    if resp.status_code == 404 and has_detail:
        # The relay itself: no such code (or a relay too old for join links).
        raise SetupError("this join link is not valid - it may be from an earlier run. Ask the"
                         " host for the link from their current run")
    if _from_the_relay(resp):
        if resp.status_code == 429:
            raise SetupError("too many tries with join links right now - wait a minute and try again")
        raise SetupError(f"the bridge refused this join link ({resp.status_code}): {_relay_detail(resp)}")
    raise RelayUnreachable(f"HTTP {resp.status_code}")


def cmd_host(name: str, allow: Optional[str], new_room: bool = False,
             clients: Optional[list[str]] = None, local: bool = False,
             max_members: Optional[int] = None) -> None:
    check_name(name)
    clients = clients or [DEFAULT_CLIENT]
    names = _client_names(name, clients)
    project = Path.cwd()
    _load_mcp_config(project / ".mcp.json")  # a broken .mcp.json stops us before anything runs
    room, token, adopted_legacy = host_room(project, new_room)
    # The join link's code stays the same across runs, like the room; a new room gets a new one.
    code = _hosted_code(project, room) if _relay_accepts("invite_codes") else None
    key = identity_key()
    members = list(dict.fromkeys(list(names.values()) + (allow.split(",") if allow else [])))
    # This relay serves exactly one room: anyone who stumbles on the tunnel URL
    # cannot create rooms of their own on it. With --allow, only those names.
    app = build_relay(room, token, members if allow else None, max_members=max_members,
                      invite_code=code)

    sock = _bind(PORT)  # if a bridge is already running, this stops us - leaving its invite alone
    port = sock.getsockname()[1]
    local_url = f"http://127.0.0.1:{port}"
    # An invite from an earlier run points at a dead tunnel. Remove it before the
    # slow part (the tunnel), so nobody - person or agent - reads it as this run's.
    _remove(INVITE_FILE)
    print("Starting bridge...\n")
    server = thread = proc = None
    invite_text = None
    try:
        with _stop_on_signals():
            server, thread = start_relay(sock, app)
            # Claim your own names (one per tool) before anyone else can reach the relay.
            for client, own in names.items():
                try:
                    register_name(local_url, room, token, own, key, client=client)
                except RelayRefused as exc:
                    hint = (" Pick another name, or start a fresh room with --new-room."
                            if exc.status == 409 else "")
                    raise SetupError(f"could not claim '{own}' on your own relay: {exc.detail}{hint}") from None
                except RelayUnreachable as exc:
                    raise SetupError(f"the relay on port {port} did not answer ({exc})") from None

            # The host reaches its own relay directly. Going out through the tunnel and
            # back would be slower, and would break outright on networks whose DNS does
            # not resolve trycloudflare.com subdomains - which some ISPs do not.
            path = write_mcp_config(local_url, room, token, name, peer=allow, key=key,
                                    hosted={"room": room, "token": token,
                                            **({"code": code} if code else {})},
                                    write_file=DEFAULT_CLIENT in clients)
            paths = {DEFAULT_CLIENT: path}
            base = load_settings(project)
            for client in clients:
                if client != DEFAULT_CLIENT:
                    paths[client] = write_client(project, client, names[client], base)[0]
            _sync_allowed_peers(project)
            if adopted_legacy:
                _remove(LEGACY_TOKEN_FILE)  # 0.2's room now belongs to this project alone
            # Before the invite exists: it means "ready".
            notes = mcp_config_warnings() + client_config_warnings(project, paths)

            proc, url = (None, None) if local else start_tunnel(port)
            if url:
                print(f"Public URL : {url}")
                problem = check_public_url(url)
                if problem:
                    print(f"  It did not answer from this machine yet ({problem}). Some networks do")
                    print("  not resolve trycloudflare.com addresses - if your friend cannot connect")
                    print("  either, see Troubleshooting in the README.")
                print()
            else:
                url = local_url
                if local:
                    print("LOCAL ONLY (--local): only programs on this computer can reach this bridge.\n")
                elif proc is None:
                    print("cloudflared not found, so this bridge is LOCAL ONLY.")
                    print("  Your friend cannot reach it until you install a tunnel:")
                    print("      winget install --id Cloudflare.cloudflared   (macOS: brew install cloudflared)")
                else:
                    print(f"cloudflared did not produce a public URL within {TUNNEL_WAIT_SECONDS:.0f} seconds,")
                    print("so this bridge is LOCAL ONLY. Check your internet connection and firewall.")
                if not local:
                    print("  then run this command again.\n")

            invite_line = f"python bridge.py join {encode_invite(url, room, token)} <theirname>"
            # Also drop the invite on disk. This command is usually launched in a
            # background shell (by a person or by an AI agent), where reading a file is
            # far more reliable than scraping a still-running process's output. It
            # lives next to this script, where .gitignore covers it, and is removed
            # when the bridge stops. With a join link, that is the first line; the
            # full invite (which works too) is the last.
            link = f"{url}/j/{code}" if code else None
            lines = [f"python bridge.py join {link} <theirname>"] if link else []
            invite_text = "\n".join(lines + [invite_line]) + "\n"
            _write_private(INVITE_FILE, invite_text)

            if clients == [DEFAULT_CLIENT]:
                print(f"Wrote {path} (you are '{name}')")
            else:
                for client in clients:
                    print(f"Wrote {paths[client]} ({CLIENTS[client].display} is '{names[client]}')")
            if allow:
                print(f"Room locked: only {', '.join(members)} can join.\n")
            else:
                print("Tip: add --allow <their name> so nobody else can join, even with the invite.\n")
            _warn_if_inside_ccbridge()
            _print_notes(notes)
            print("=" * 62)
            if link:
                print("SEND THIS LINK TO YOUR FRIEND (privately - it lets them into the room):")
                print()
                print(f"  Join link: {link}")
                print()
                print("They run:  python bridge.py join <the link> <theirname>")
                print("(or open the link: it shows the steps, which they can also hand to")
                print(" their AI agent). The full invite works too:")
                print(f"  {invite_line}")
            else:
                print("SEND THIS ONE LINE TO YOUR FRIEND (privately - it holds the room token):")
                print()
                print(f"  {invite_line}")
            print()
            print("=" * 62)
            print(f"\nAlso saved to: {INVITE_FILE}")
            restart = ", ".join(CLIENTS[c].display for c in clients)
            print(f"Now restart {restart} in this folder and approve the ccbridge MCP server.")
            print("Keep this window open. Ctrl+C to stop the bridge.\n")
            serve_until_stopped(thread)
    finally:
        if server is not None:
            stop_relay(server, thread)
        sock.close()
        if proc is not None:
            _stop(proc)
        # The invite dies with the tunnel. Leave the file alone if another run
        # has written its own since.
        if invite_text is not None and _read_or_none(INVITE_FILE) == invite_text:
            _remove(INVITE_FILE)


# --- join ----------------------------------------------------------------------


def _join_refused(exc: RelayRefused) -> SetupError:
    if exc.status == 403 and exc.detail in ("Unknown room", "Bad room token"):
        return SetupError("the bridge no longer accepts this invite - ask the host for"
                          " the invite from their current run")
    if exc.status == 409:
        return SetupError(f"{exc.detail} Run join again with another name.")
    if exc.status == 429:
        return SetupError("the bridge is busy - try again in a minute")
    return SetupError(f"the bridge refused to let you in ({exc.status}): {exc.detail}")


# join's exit code when the settings were saved but no bridge answered.
EXIT_UNREACHABLE = 2


def cmd_join(invite: str, name: str, allow: Optional[str],
             clients: Optional[list[str]] = None) -> int:
    """Join; returns the exit code (0, or EXIT_UNREACHABLE)."""
    check_name(name)
    clients = clients or [DEFAULT_CLIENT]
    names = _client_names(name, clients)
    project = Path.cwd()
    link = parse_join_link(invite)
    if link is None:
        url, room, token = decode_invite(invite)
    _load_mcp_config(project / ".mcp.json")  # refuse a broken file before contacting anyone
    if link is not None:
        url = link[0]
        try:
            room, token = redeem_join_link(*link)
        except RelayUnreachable as exc:
            # Unlike with a full invite, there is nothing to save: the room and
            # token come from the bridge.
            print(f"No bridge answered at {url} ({exc}).")
            print("  Usually the host's bridge has stopped, or this link is from an earlier run -")
            print("  a quick tunnel gets a new address every run. Ask the host for the link from")
            print("  their current run. Nothing was saved.")
            return EXIT_UNREACHABLE
    key = identity_key()
    # Claim the names now: a taken name or a stale invite shows up here, not
    # later inside Claude Code.
    unreachable = _claim_names(url, room, token, names, key)

    path = write_mcp_config(url, room, token, name, peer=allow, key=key,
                            write_file=DEFAULT_CLIENT in clients)
    if clients != [DEFAULT_CLIENT]:
        base = load_settings(project)
        paths = {c: (path if c == DEFAULT_CLIENT else write_client(project, c, names[c], base)[0])
                 for c in clients}
    _sync_allowed_peers(project)
    if unreachable:
        print(f"No bridge answered at {url} ({unreachable}).")
        print("  Usually the host's bridge has stopped, or this invite is from an earlier")
        print("  run - a quick tunnel gets a new address every run. Ask the host for the")
        print("  invite from their current run. (If it is current, see Troubleshooting in")
        print("  the README.) Saved the settings anyway, in case the bridge comes back.\n")
    else:
        print(f"Joined bridge room '{room}' as '{', '.join(names.values())}'.")
    if clients == [DEFAULT_CLIENT]:
        print(f"Wrote {path}\n")
        _warn_if_inside_ccbridge()
        _print_notes(mcp_config_warnings())
        print("Now restart Claude Code in this folder, approve the 'ccbridge' MCP server")
        print("when it asks, then ask it:")
    else:
        _warn_if_inside_ccbridge()
        _print_notes(mcp_config_warnings() + client_config_warnings(project, paths))
        _print_restart(clients, paths)
        print("Then ask any of them:")
    print('    "check the bridge - who is online?"')
    return EXIT_UNREACHABLE if unreachable else 0


def cmd_connect(raw_clients: str, name: Optional[str]) -> int:
    """Add more tools in this project to the bridge it already uses."""
    try:
        clients = parse_clients(raw_clients, find_program)
    except ClientError as exc:
        raise SetupError(str(exc)) from None
    project = Path.cwd()
    base = load_settings(project)
    if not all(isinstance(base.get(k), str) for k in
               ("CCBRIDGE_RELAY_URL", "CCBRIDGE_ROOM", "CCBRIDGE_TOKEN", "CCBRIDGE_NAME")):
        raise SetupError("this project is not on a bridge yet - run `bridge.py host` or"
                         " `bridge.py join` in it first")
    if name:
        check_name(name)
    names = _client_names(base["CCBRIDGE_NAME"], clients, override=name)
    key = base.get("CCBRIDGE_IDENTITY_KEY") or identity_key()
    unreachable = _claim_names(base["CCBRIDGE_RELAY_URL"], base["CCBRIDGE_ROOM"],
                               base["CCBRIDGE_TOKEN"], names, key)
    paths = {}
    for client in clients:
        paths[client], copy = write_client(project, client, names[client], base)
        print(f"{CLIENTS[client].display}: '{names[client]}'")
        if copy:
            print(f"  (the previous file was saved as {copy})")
    _sync_allowed_peers(project)
    if unreachable:
        print(f"\nNo bridge answered at {base['CCBRIDGE_RELAY_URL']} ({unreachable}); the names"
              " are claimed when the bridge is back. Saved the settings anyway.")
    print()
    _print_notes(client_config_warnings(project, paths))
    _print_restart(clients, paths)
    return EXIT_UNREACHABLE if unreachable else 0


def cmd_clients() -> None:
    """List the tools ccbridge can set up, and which of them are installed here."""
    project = Path.cwd()
    print(f"{'client':<13}{'installed':<11}{'checked':<9}config file")
    for slug, spec in CLIENTS.items():
        found = "yes" if is_installed(spec, find_program) else "-"
        print(f"{slug:<13}{found:<11}{spec.source:<9}{config_path(spec, project)}")
    print("\n'tested' = set up and seen working here; 'docs' = set up per the tool's docs.")
    print("Use: bridge.py connect --client cursor,codex   (or --client all-installed)")


def _allow_list(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    names = [check_name(n.strip()) for n in raw.split(",") if n.strip()]
    return ",".join(names) or None


def main(argv: Optional[list[str]] = None) -> None:
    # Python block-buffers stdout when it is not a terminal, so running `host` in
    # a background shell would hide the invite line until the buffer filled.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:  # pragma: no cover - very old Python
        pass

    parser = argparse.ArgumentParser(
        prog="bridge.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    host = sub.add_parser("host", help="start the relay and print an invite")
    host.add_argument("name", help="how you appear to your peer")
    host.add_argument("--new-room", action="store_true",
                      help="start a fresh room and token; earlier invites stop working")
    join = sub.add_parser("join", help="connect to someone else's relay")
    join.add_argument("invite", help="the join link (https://.../j/XXXX-XXXX) or invite string"
                                     " from the host")
    join.add_argument("name", help="how you appear to your peer")
    host.add_argument("--allow", metavar="NAME",
                      help="only these peers (comma-separated) may join, and only their"
                           " messages are accepted")
    join.add_argument("--allow", metavar="NAME",
                      help="only accept messages from this peer (comma-separate several)")
    client_help = (f"which AI tools in this project to set up: comma-separated, or"
                   f" all-installed (default {DEFAULT_CLIENT}; see `bridge.py clients`)")
    for p in (host, join):
        p.add_argument("--client", metavar="CLIENT", help=client_help)
    host.add_argument("--local", action="store_true",
                      help="no tunnel: only programs on this computer can reach the bridge")
    host.add_argument("--max-members", type=int, metavar="N",
                      help="let at most N members into the room")
    connect = sub.add_parser("connect", help="set up more AI tools in a project that already"
                                             " ran host or join")
    connect.add_argument("--client", metavar="CLIENT", required=True, help=client_help)
    connect.add_argument("--name", help="the name to appear under (default <your name>-<client>;"
                                        " with several clients, used in place of your name)")
    sub.add_parser("clients", help="list the AI tools ccbridge can set up")
    args = parser.parse_args(argv)

    code = 0
    try:
        if args.command == "clients":
            cmd_clients()
            return
        if args.command == "connect":
            code = cmd_connect(args.client, args.name)
        else:
            allow = _allow_list(args.allow)
            try:
                clients = parse_clients(args.client, find_program)
            except ClientError as exc:
                raise SetupError(str(exc)) from None
            if args.command == "host":
                if args.max_members is not None and args.max_members < 1:
                    raise SetupError("--max-members must be at least 1")
                cmd_host(args.name, allow, new_room=args.new_room, clients=clients,
                         local=args.local, max_members=args.max_members)
            else:
                code = cmd_join(args.invite, args.name, allow, clients=clients)
    except SetupError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()
