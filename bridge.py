"""One-command setup for ccbridge.

    python bridge.py host <yourname>      # you: starts everything, prints an invite
    python bridge.py join <invite> <name> # your friend: pastes the invite, done

Everything either side needs - relay URL, room, token - is packed into the single
invite string, so nobody has to copy three separate values into a config file.
"""

from __future__ import annotations

import base64
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

HERE = Path(__file__).resolve().parent
PORT = int(os.getenv("CCBRIDGE_PORT", "8787"))
TUNNEL_URL_RE = re.compile(rb"https://[a-z0-9-]+\.trycloudflare\.com")


def encode_invite(url: str, room: str, token: str) -> str:
    blob = json.dumps({"u": url, "r": room, "t": token}, separators=(",", ":"))
    return base64.urlsafe_b64encode(blob.encode()).decode().rstrip("=")


def decode_invite(invite: str) -> tuple[str, str, str]:
    padded = invite.strip() + "=" * (-len(invite.strip()) % 4)
    data = json.loads(base64.urlsafe_b64decode(padded))
    return data["u"], data["r"], data["t"]


def write_mcp_config(url: str, room: str, token: str, name: str, peer: str | None) -> Path:
    """Write .mcp.json in the current directory so Claude Code picks the bridge up."""
    path = Path.cwd() / ".mcp.json"
    config = json.loads(path.read_text()) if path.exists() else {}
    env = {
        "PYTHONPATH": str(HERE),
        "CCBRIDGE_RELAY_URL": url,
        "CCBRIDGE_ROOM": room,
        "CCBRIDGE_TOKEN": token,
        "CCBRIDGE_NAME": name,
    }
    if peer:
        env["CCBRIDGE_ALLOWED_PEERS"] = peer
    config.setdefault("mcpServers", {})["ccbridge"] = {
        "command": sys.executable,
        "args": ["-m", "ccbridge.mcp_server"],
        "env": env,
    }
    path.write_text(json.dumps(config, indent=2))

    # The token lives in this file, so keep it out of git.
    ignore = Path.cwd() / ".gitignore"
    existing = ignore.read_text() if ignore.exists() else ""
    if ".mcp.json" not in existing:
        ignore.write_text(existing.rstrip("\n") + "\n.mcp.json\n" if existing else ".mcp.json\n")
    return path


def start_tunnel() -> str | None:
    """Start cloudflared and return the public URL, or None if it isn't installed."""
    if not shutil.which("cloudflared"):
        return None
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://localhost:{PORT}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    url: list[str] = []

    def read() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            match = TUNNEL_URL_RE.search(line)
            if match and not url:
                url.append(match.group(0).decode())

    threading.Thread(target=read, daemon=True).start()
    for _ in range(300):
        if url:
            return url[0]
        time.sleep(0.1)
    return None


def cmd_host(name: str) -> None:
    token_file = HERE / ".ccbridge_token"
    if token_file.exists():
        room, token = token_file.read_text().split("\n")[:2]
    else:
        room, token = f"bridge-{secrets.token_hex(4)}", secrets.token_urlsafe(32)
        token_file.write_text(f"{room}\n{token}\n")

    print("Starting bridge...\n")
    url = start_tunnel()
    if url:
        print(f"Public URL : {url}")
    else:
        url = f"http://localhost:{PORT}"
        print("cloudflared not found, so this bridge is LOCAL ONLY.")
        print("  Your friend cannot reach it until you install a tunnel:")
        print("      winget install --id Cloudflare.cloudflared")
        print("  then run this command again.\n")

    # The host reaches its own relay directly. Going out through the tunnel and
    # back would be slower, and would break outright on networks whose DNS does
    # not resolve trycloudflare.com subdomains - which some ISPs do not.
    write_mcp_config(f"http://127.0.0.1:{PORT}", room, token, name, peer=None)
    invite_line = f"python bridge.py join {encode_invite(url, room, token)} <theirname>"

    # Also drop the invite on disk. This command is usually launched in a
    # background shell (by a person or by an AI agent), where reading a file is
    # far more reliable than scraping a still-running process's output.
    invite_file = Path.cwd() / "invite.txt"
    invite_file.write_text(invite_line + "\n")

    print(f"Wrote {Path.cwd() / '.mcp.json'} (you are '{name}')\n")
    print("=" * 62)
    print("SEND THIS ONE LINE TO YOUR FRIEND:")
    print()
    print(f"  {invite_line}")
    print()
    print("=" * 62)
    print(f"\nAlso saved to: {invite_file}")
    print("Keep this window open. Ctrl+C to stop the bridge.\n")

    os.environ["CCBRIDGE_DB"] = str(HERE / "ccbridge.db")
    sys.path.insert(0, str(HERE))
    import uvicorn

    uvicorn.run("ccbridge.relay:app", host="127.0.0.1", port=PORT, log_level="warning")


def cmd_join(invite: str, name: str) -> None:
    url, room, token = decode_invite(invite)
    path = write_mcp_config(url, room, token, name, peer=None)
    print(f"Joined bridge room '{room}' as '{name}'.")
    print(f"Wrote {path}")
    print("\nNow restart Claude Code in this folder, then ask it:")
    print('    "check the bridge - who is online?"')


def main() -> None:
    # Python block-buffers stdout when it is not a terminal, so running `host` in
    # a background shell would hide the invite line until the buffer filled.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:  # pragma: no cover - very old Python
        pass

    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "host":
        cmd_host(args[1])
    elif len(args) == 3 and args[0] == "join":
        cmd_join(args[1], args[2])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
