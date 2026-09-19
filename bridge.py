"""One-command setup for ccbridge. Run it from the project folder you will open
Claude Code in - it writes .mcp.json there.

    python <ccbridge>/bridge.py host <yourname>           # you: starts everything, prints an invite
    python <ccbridge>/bridge.py join <invite> <yourname>  # your friend: pastes the invite, done

Add --allow <name> to either to accept messages from that peer only.

Everything either side needs - relay URL, room, token - is packed into the single
invite string, so nobody has to copy three separate values into a config file.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from ccbridge.safety import NAME_RE, ROOM_RE

HERE = Path(__file__).resolve().parent
PORT = int(os.getenv("CCBRIDGE_PORT", "8787"))
TUNNEL_URL_RE = re.compile(rb"https://[a-z0-9-]+\.trycloudflare\.com")
TUNNEL_WAIT_SECONDS = 30.0

# All three hold secrets, live next to this script, and are gitignored there.
TOKEN_FILE = HERE / ".ccbridge_token"
IDENTITY_FILE = HERE / ".ccbridge_identity"
INVITE_FILE = HERE / "invite.txt"


class SetupError(Exception):
    """A problem the person running this can fix; printed without a traceback."""


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


def identity_key() -> str:
    """This machine's identity key, created on first use.

    It owns your display name in every room you join from this ccbridge folder,
    so rejoining with a fresh invite keeps your name. Keep it private.
    """
    if IDENTITY_FILE.exists():
        key = IDENTITY_FILE.read_text(encoding="utf-8").strip()
        if len(key) >= 16:
            return key
    key = secrets.token_urlsafe(32)
    IDENTITY_FILE.write_text(key + "\n", encoding="utf-8")
    return key


def _ensure_gitignored(entry: str) -> None:
    ignore = Path.cwd() / ".gitignore"
    # surrogateescape: never choke on, or mangle, a .gitignore that is not UTF-8.
    existing = ignore.read_text(encoding="utf-8", errors="surrogateescape") if ignore.exists() else ""
    lines = {line.strip() for line in existing.splitlines()}
    if entry in lines or f"/{entry}" in lines:
        return
    updated = existing.rstrip("\n") + "\n" + entry + "\n" if existing.strip() else entry + "\n"
    ignore.write_text(updated, encoding="utf-8", errors="surrogateescape")


def write_mcp_config(url: str, room: str, token: str, name: str, peer: str | None,
                     key: str | None = None) -> Path:
    """Write .mcp.json in the current directory so Claude Code picks the bridge up."""
    path = Path.cwd() / ".mcp.json"
    config: object = {}
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise SetupError(f"{path} exists but is not valid JSON ({exc}); fix or remove it") from None
    servers = config.setdefault("mcpServers", {}) if isinstance(config, dict) else None
    if not isinstance(servers, dict):
        raise SetupError(f"{path} has an unexpected shape; fix or remove it")

    env = {
        "PYTHONPATH": str(HERE),
        "CCBRIDGE_RELAY_URL": url,
        "CCBRIDGE_ROOM": room,
        "CCBRIDGE_TOKEN": token,
        "CCBRIDGE_NAME": name,
        "CCBRIDGE_IDENTITY_KEY": key or identity_key(),
    }
    if peer:
        env["CCBRIDGE_ALLOWED_PEERS"] = peer
    servers["ccbridge"] = {
        "command": sys.executable,
        "args": ["-m", "ccbridge.mcp_server"],
        "env": env,
    }
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    # The token lives in this file, so keep it out of git.
    _ensure_gitignored(".mcp.json")
    return path


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def start_tunnel() -> tuple[subprocess.Popen | None, str | None]:
    """Start cloudflared and return ``(process, public URL)``.

    ``(None, None)`` means cloudflared is not installed. ``(process, None)`` means
    it is installed but produced no URL in time; the process is already stopped.
    """
    exe = shutil.which("cloudflared")
    if not exe:
        return None, None
    proc = subprocess.Popen(
        [exe, "tunnel", "--url", f"http://localhost:{PORT}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    url: list[str] = []

    def read() -> None:
        # Keep draining after the URL shows up, or cloudflared blocks on a full pipe.
        for line in proc.stdout:  # type: ignore[union-attr]
            match = TUNNEL_URL_RE.search(line)
            if match and not url:
                url.append(match.group(0).decode())

    threading.Thread(target=read, daemon=True).start()
    deadline = time.monotonic() + TUNNEL_WAIT_SECONDS
    while time.monotonic() < deadline and not url and proc.poll() is None:
        time.sleep(0.1)
    if url:
        return proc, url[0]
    _stop(proc)
    return proc, None


def _host_credentials() -> tuple[str, str]:
    if TOKEN_FILE.exists():
        parts = TOKEN_FILE.read_text(encoding="utf-8").split()
        if len(parts) >= 2 and ROOM_RE.match(parts[0]):
            return parts[0], parts[1]
        raise SetupError(f"{TOKEN_FILE} is damaged - delete it and run host again"
                         " (your friend will then need the new invite)")
    room, token = f"bridge-{secrets.token_hex(4)}", secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(f"{room}\n{token}\n", encoding="utf-8")
    return room, token


def _warn_if_inside_ccbridge() -> None:
    if Path.cwd().resolve() == HERE:
        print("Note: you ran this inside the ccbridge folder, so the bridge only loads when")
        print("      Claude Code is opened here. To use it in your project, run this from")
        print("      the project folder instead.\n")


def cmd_host(name: str, allow: str | None) -> None:
    check_name(name)
    room, token = _host_credentials()

    print("Starting bridge...\n")
    proc, url = start_tunnel()
    try:
        if url:
            print(f"Public URL : {url}")
        else:
            url = f"http://localhost:{PORT}"
            if proc is None:
                print("cloudflared not found, so this bridge is LOCAL ONLY.")
                print("  Your friend cannot reach it until you install a tunnel:")
                print("      winget install --id Cloudflare.cloudflared   (macOS: brew install cloudflared)")
            else:
                print(f"cloudflared did not produce a public URL within {TUNNEL_WAIT_SECONDS:.0f} seconds,")
                print("so this bridge is LOCAL ONLY. Check your internet connection and firewall.")
            print("  then run this command again.\n")

        # The host reaches its own relay directly. Going out through the tunnel and
        # back would be slower, and would break outright on networks whose DNS does
        # not resolve trycloudflare.com subdomains - which some ISPs do not.
        path = write_mcp_config(f"http://127.0.0.1:{PORT}", room, token, name, peer=allow)
        invite_line = f"python bridge.py join {encode_invite(url, room, token)} <theirname>"

        # Also drop the invite on disk. This command is usually launched in a
        # background shell (by a person or by an AI agent), where reading a file is
        # far more reliable than scraping a still-running process's output. It
        # lives next to this script, where .gitignore already covers it.
        INVITE_FILE.write_text(invite_line + "\n", encoding="utf-8")

        print(f"Wrote {path} (you are '{name}')\n")
        _warn_if_inside_ccbridge()
        print("=" * 62)
        print("SEND THIS ONE LINE TO YOUR FRIEND (privately - it holds the room token):")
        print()
        print(f"  {invite_line}")
        print()
        print("=" * 62)
        print(f"\nAlso saved to: {INVITE_FILE}")
        print("Now restart Claude Code in this folder and approve the 'ccbridge' MCP server.")
        print("Keep this window open. Ctrl+C to stop the bridge.\n")

        os.environ["CCBRIDGE_DB"] = str(HERE / "ccbridge.db")
        # This relay serves exactly one room. Anyone who stumbles on the tunnel
        # URL cannot create rooms of their own on it.
        os.environ["CCBRIDGE_ROOMS"] = f"{room}:{token}"
        os.environ["CCBRIDGE_OPEN_ROOMS"] = "0"
        sys.path.insert(0, str(HERE))
        import uvicorn

        uvicorn.run("ccbridge.relay:app_from_env", factory=True,
                    host="127.0.0.1", port=PORT, log_level="warning")
    finally:
        if proc is not None:
            _stop(proc)


def cmd_join(invite: str, name: str, allow: str | None) -> None:
    check_name(name)
    url, room, token = decode_invite(invite)
    path = write_mcp_config(url, room, token, name, peer=allow)
    print(f"Joined bridge room '{room}' as '{name}'.")
    print(f"Wrote {path}\n")
    _warn_if_inside_ccbridge()
    print("Now restart Claude Code in this folder, approve the 'ccbridge' MCP server")
    print("when it asks, then ask it:")
    print('    "check the bridge - who is online?"')


def _allow_list(raw: str | None) -> str | None:
    if not raw:
        return None
    names = [check_name(n.strip()) for n in raw.split(",") if n.strip()]
    return ",".join(names) or None


def main(argv: list[str] | None = None) -> None:
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
    join = sub.add_parser("join", help="connect to someone else's relay")
    join.add_argument("invite", help="the invite string from the host")
    join.add_argument("name", help="how you appear to your peer")
    for command in (host, join):
        command.add_argument("--allow", metavar="NAME",
                             help="only accept messages from this peer (comma-separate several)")
    args = parser.parse_args(argv)

    try:
        allow = _allow_list(args.allow)
        if args.command == "host":
            cmd_host(args.name, allow)
        else:
            cmd_join(args.invite, args.name, allow)
    except SetupError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
