import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys

import httpx
import pytest

import bridge
from bridge import SetupError, decode_invite, encode_invite, write_mcp_config
from ccbridge import config
from ccbridge.identity import join_body
from ccbridge.net import http_client

POSIX = os.name != "nt"


def test_invite_round_trip():
    url, room, token = "https://x.trycloudflare.com", "bridge-ab12", "tok_123-_"
    assert decode_invite(encode_invite(url, room, token)) == (url, room, token)


def test_invite_survives_whitespace_from_copy_paste():
    invite = encode_invite("https://x.trycloudflare.com", "room", "tok")
    assert decode_invite(f"  {invite}\n") == ("https://x.trycloudflare.com", "room", "tok")


@pytest.mark.parametrize("junk", ["", "not-an-invite", "eyJ1IjoxfQ", encode_invite("ftp://x", "room", "t"),
                                  encode_invite("https://x", "bad room!", "t")])
def test_bad_invite_is_a_friendly_error(junk):
    with pytest.raises(SetupError, match="invite is not valid"):
        decode_invite(junk)


@pytest.fixture()
def in_tmp(tmp_path, monkeypatch):
    """Run in a scratch project folder, with this checkout's secret files redirected."""
    project, home = tmp_path / "project", tmp_path / "ccbridge"
    project.mkdir()
    home.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(bridge, "IDENTITY_FILE", home / ".ccbridge_identity")
    monkeypatch.setattr(bridge, "PROJECTS_DIR", home / ".ccbridge_projects")
    monkeypatch.setattr(bridge, "INVITE_FILE", home / "invite.txt")
    monkeypatch.setattr(bridge, "DB_FILE", home / "ccbridge.db")
    monkeypatch.setattr(bridge, "LEGACY_TOKEN_FILE", home / ".ccbridge_token")
    monkeypatch.setattr(bridge, "PORT", 0)  # any free port
    # cmd_host configures the relay through the environment; undo that afterwards.
    # (delenv alone does not record a variable that was never set, so set it first.)
    for var in ("CCBRIDGE_DB", "CCBRIDGE_ROOMS", "CCBRIDGE_OPEN_ROOMS", "CCBRIDGE_ALLOWED_NAMES",
                "CCBRIDGE_CONFIG"):
        monkeypatch.setenv(var, "")
        monkeypatch.delenv(var)
    return project


def settings(project):
    return bridge.load_settings(project)


@pytest.fixture()
def host(in_tmp, monkeypatch):
    """Run ``bridge.py host`` with a real relay on a free port, and no tunnel.

    ``while_up`` runs while the bridge serves, in place of waiting for Ctrl+C;
    its return value is returned.
    """
    monkeypatch.setattr(bridge, "start_tunnel", lambda port=0: (None, None))

    def run(name="sourabh", allow=None, new_room=False, while_up=lambda: None):
        seen = {}

        def serve(thread):
            seen["result"] = while_up()

        monkeypatch.setattr(bridge, "serve_until_stopped", serve)
        bridge.cmd_host(name, allow, new_room=new_room)
        return seen.get("result")

    return run


def relay_of(project):
    s = settings(project)
    return s["CCBRIDGE_RELAY_URL"], s["CCBRIDGE_ROOM"], s["CCBRIDGE_TOKEN"]


def join_as(project, name, key, register_only=False, room=None):
    url, own_room, token = relay_of(project)
    with http_client(timeout=10.0) as http:
        return http.post(f"{url}/v1/join",
                         json=join_body(room or own_room, name, key, register_only=register_only),
                         headers={"Authorization": f"Bearer {token}"})


# --- .mcp.json and the settings file -------------------------------------------


def test_write_mcp_config_shape(in_tmp):
    write_mcp_config("https://relay", "room1", "tok", "friend", peer="sourabh")
    cfg = json.loads((in_tmp / ".mcp.json").read_text(encoding="utf-8"))

    server = cfg["mcpServers"]["ccbridge"]
    assert server["args"] == [str(bridge.LAUNCHER)] and bridge.LAUNCHER.is_file()
    assert server["env"] == {"CCBRIDGE_CONFIG": str(bridge.settings_path(in_tmp))}
    s = settings(in_tmp)
    assert s["CCBRIDGE_RELAY_URL"] == "https://relay"
    assert s["CCBRIDGE_NAME"] == "friend"
    assert s["CCBRIDGE_ALLOWED_PEERS"] == "sourabh"
    assert len(s["CCBRIDGE_IDENTITY_KEY"]) >= 16


def test_mcp_json_holds_no_secret(in_tmp):
    """Regression: .mcp.json held the room token and identity key, and many projects commit it."""
    token, key = "room-token-0123456789", "identity-key-0123456789"
    write_mcp_config("https://relay", "room1", token, "me", peer=None, key=key)

    text = (in_tmp / ".mcp.json").read_text(encoding="utf-8")
    assert token not in text and key not in text
    stored = settings(in_tmp)
    assert stored["CCBRIDGE_TOKEN"] == token and stored["CCBRIDGE_IDENTITY_KEY"] == key
    # ...and the settings file lives in the ccbridge folder, not in the project.
    assert bridge.PROJECTS_DIR in bridge.settings_path(in_tmp).parents


def test_each_project_gets_its_own_settings(in_tmp, monkeypatch):
    write_mcp_config("https://relay", "room-a", "tok", "me", peer=None)
    other = in_tmp.parent / "other-project"
    other.mkdir()
    monkeypatch.chdir(other)
    write_mcp_config("https://relay", "room-b", "tok", "me", peer=None)
    assert settings(in_tmp)["CCBRIDGE_ROOM"] == "room-a"
    assert settings(other)["CCBRIDGE_ROOM"] == "room-b"


def test_identity_key_is_stable_across_invites(in_tmp):
    """A fresh invite (new tunnel URL) must not cost you your display name."""
    write_mcp_config("https://one", "room1", "tok", "me", peer=None)
    first = settings(in_tmp)["CCBRIDGE_IDENTITY_KEY"]
    write_mcp_config("https://two", "room1", "tok", "me", peer=None)
    assert settings(in_tmp)["CCBRIDGE_IDENTITY_KEY"] == first


def test_upgrading_drops_the_old_secrets_but_keeps_your_own_settings(in_tmp):
    old_entry = {"command": "python", "args": ["-m", "ccbridge.mcp_server"],
                 "env": {"PYTHONPATH": "x", "CCBRIDGE_TOKEN": "old-token", "CCBRIDGE_ROOM": "r",
                         "CCBRIDGE_IDENTITY_KEY": "old-key", "CCBRIDGE_CONFIRM_SENDS": "1"}}
    (in_tmp / ".mcp.json").write_text(json.dumps({"mcpServers": {"ccbridge": old_entry}}),
                                      encoding="utf-8")
    write_mcp_config("https://relay", "room1", "tok", "me", peer=None)
    env = json.loads((in_tmp / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["ccbridge"]["env"]
    assert env == {"CCBRIDGE_CONFIRM_SENDS": "1", "CCBRIDGE_CONFIG": str(bridge.settings_path(in_tmp))}


@pytest.mark.skipif(not POSIX, reason="file modes are a macOS/Linux thing")
def test_secret_files_are_readable_only_by_you(in_tmp, host):
    bridge.IDENTITY_FILE.write_text("an-identity-key-from-0.2-0123\n", encoding="utf-8")
    os.chmod(bridge.IDENTITY_FILE, 0o644)  # how versions before 0.3 left it

    def modes():
        return {p: stat.S_IMODE(p.stat().st_mode) for p in
                (bridge.IDENTITY_FILE, bridge.settings_path(in_tmp), bridge.INVITE_FILE, bridge.DB_FILE)}

    assert set(host(while_up=modes).values()) == {0o600}


def test_token_file_is_gitignored(in_tmp):
    write_mcp_config("https://relay", "room1", "tok", "me", peer=None)
    assert ".mcp.json" in (in_tmp / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_gitignore_entry_is_not_duplicated(in_tmp):
    for _ in range(2):
        write_mcp_config("https://relay", "room1", "tok", "me", peer=None)
    assert (in_tmp / ".gitignore").read_text(encoding="utf-8").splitlines().count(".mcp.json") == 1


def test_gitignore_mentioning_the_name_in_a_comment_still_gets_the_entry(in_tmp):
    (in_tmp / ".gitignore").write_text("# see .mcp.json.example\n", encoding="utf-8")
    write_mcp_config("https://relay", "room1", "tok", "me", peer=None)
    assert ".mcp.json" in (in_tmp / ".gitignore").read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("eol", [b"\n", b"\r\n"])
def test_line_endings_are_kept(in_tmp, eol):
    """Regression: on Windows an LF .gitignore was rewritten with CRLF throughout."""
    (in_tmp / ".gitignore").write_bytes(b"*.pyc" + eol + b"build/" + eol)
    (in_tmp / ".mcp.json").write_bytes(b'{' + eol + b'  "mcpServers": {}' + eol + b'}' + eol)
    write_mcp_config("https://relay", "room1", "tok", "me", peer=None)

    for name in (".gitignore", ".mcp.json"):
        data = (in_tmp / name).read_bytes()
        if eol == b"\n":
            assert b"\r" not in data, name
        else:
            assert data.count(b"\n") == data.count(b"\r\n"), name
    assert (in_tmp / ".gitignore").read_bytes() == b"*.pyc" + eol + b"build/" + eol + b".mcp.json" + eol


def test_gitignore_without_a_final_newline_gets_the_entry_on_its_own_line(in_tmp):
    (in_tmp / ".gitignore").write_bytes(b"*.pyc")
    write_mcp_config("https://relay", "room1", "tok", "me", peer=None)
    assert (in_tmp / ".gitignore").read_bytes() == b"*.pyc\n.mcp.json\n"


def test_existing_mcp_servers_are_preserved(in_tmp):
    (in_tmp / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}),
                                      encoding="utf-8")
    write_mcp_config("https://relay", "room1", "tok", "me", peer=None)

    cfg = json.loads((in_tmp / ".mcp.json").read_text(encoding="utf-8"))
    assert "other" in cfg["mcpServers"]
    assert "ccbridge" in cfg["mcpServers"]


def test_mcp_config_saved_with_a_byte_order_mark_is_read(in_tmp):
    """Regression: Notepad and PowerShell 5 save UTF-8 with a BOM, which was refused."""
    (in_tmp / ".mcp.json").write_bytes(
        b"\xef\xbb\xbf" + json.dumps({"mcpServers": {"other": {"command": "x"}}}).encode())
    write_mcp_config("https://relay", "room1", "tok", "me", peer=None)

    data = (in_tmp / ".mcp.json").read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert set(json.loads(data)["mcpServers"]) == {"other", "ccbridge"}


def test_empty_mcp_config_is_filled_in(in_tmp):
    (in_tmp / ".mcp.json").write_text("", encoding="utf-8")
    write_mcp_config("https://relay", "room1", "tok", "me", peer=None)
    assert "ccbridge" in json.loads((in_tmp / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]


def test_existing_utf8_mcp_config_is_read_correctly(in_tmp):
    (in_tmp / ".mcp.json").write_text(json.dumps({"mcpServers": {"x": {"command": "café"}}},
                                                 ensure_ascii=False), encoding="utf-8")
    write_mcp_config("https://relay", "room1", "tok", "me", peer=None)
    cfg = json.loads((in_tmp / ".mcp.json").read_text(encoding="utf-8"))
    assert cfg["mcpServers"]["x"]["command"] == "café"


def test_broken_mcp_config_is_not_overwritten(in_tmp):
    (in_tmp / ".mcp.json").write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(SetupError, match="not valid JSON"):
        write_mcp_config("https://relay", "room1", "tok", "me", peer=None)
    assert (in_tmp / ".mcp.json").read_text(encoding="utf-8") == "{ this is not json"
    assert not bridge.settings_path(in_tmp).exists()


def test_existing_gitignore_is_appended_not_replaced(in_tmp):
    (in_tmp / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    write_mcp_config("https://relay", "room1", "tok", "me", peer=None)

    content = (in_tmp / ".gitignore").read_text(encoding="utf-8")
    assert "*.pyc" in content and ".mcp.json" in content


def test_repo_gitignore_covers_every_secret_file():
    lines = (bridge.HERE / ".gitignore").read_text(encoding="utf-8").splitlines()
    for secret in (".ccbridge_projects/", ".ccbridge_token", ".ccbridge_identity", "invite.txt",
                   ".mcp.json", "*.db", "*.db-wal", "*.db-shm"):
        assert secret in lines


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_tracked_mcp_config_is_pointed_out(in_tmp):
    subprocess.run(["git", "init", "-q"], cwd=in_tmp, check=True)
    (in_tmp / ".mcp.json").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "add", ".mcp.json"], cwd=in_tmp, check=True)

    notes = bridge.mcp_config_warnings()
    assert any("tracked by git" in n for n in notes)


def test_a_token_in_git_history_is_pointed_out(in_tmp, monkeypatch):
    def fake_git(*args):
        out = "abc1234\n" if args[0] == "log" else ""
        return subprocess.CompletedProcess(args, 0 if args[0] == "log" else 1, out, "")

    monkeypatch.setattr(bridge, "_git", fake_git)
    notes = bridge.mcp_config_warnings()
    assert len(notes) == 1 and "abc1234" in notes[0] and "--new-room" in notes[0]


def test_no_notes_outside_git(in_tmp):
    assert bridge.mcp_config_warnings() == []


# --- the launcher ----------------------------------------------------------------


def test_launcher_ignores_modules_in_the_project(in_tmp):
    """Regression: `python -m` imported a project's own `mcp` folder instead of the MCP SDK."""
    (in_tmp / "mcp").mkdir()
    (in_tmp / "mcp" / "__init__.py").write_text('raise SystemExit("SHADOWED")\n', encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CCBRIDGE_", "PYTHONPATH"))}

    # Claude Code would keep stdin open; closing it makes the server exit at once.
    done = subprocess.run([sys.executable, str(bridge.LAUNCHER)], cwd=in_tmp, env=env,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    assert "SHADOWED" not in done.stdout + done.stderr
    assert done.returncode == 0, done.stderr


# --- host --------------------------------------------------------------------------


def test_tunnel_url_ignores_the_api_address():
    """Regression: cloudflared's error line was read as the public URL."""
    error = b'ERR failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": EOF'
    assert bridge.tunnel_url_in(error) is None
    assert bridge.tunnel_url_in(b"INF |  https://seasonal-deck-organisms-sf.trycloudflare.com  |") \
        == "https://seasonal-deck-organisms-sf.trycloudflare.com"


def test_invite_file_is_not_written_into_the_project(in_tmp, host):
    """Regression: invite.txt (which holds the token) used to land, un-ignored, in the project."""
    def check():
        assert not (in_tmp / "invite.txt").exists()
        return bridge.INVITE_FILE.read_text(encoding="utf-8")

    assert "python bridge.py join " in host(while_up=check)


def test_stopping_the_host_removes_the_invite_and_stops_the_relay(in_tmp, host):
    host()
    assert not bridge.INVITE_FILE.exists()
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"{relay_of(in_tmp)[0]}/healthz", timeout=2.0)


def test_host_locks_the_relay_to_its_own_room(in_tmp, host):
    """Anyone who finds the tunnel URL cannot create rooms of their own on it."""
    def other_room():
        return join_as(in_tmp, "alex", "key-of-alex-0123456789", room="a-room-of-my-own")

    resp = host(while_up=other_room)
    assert resp.status_code == 403 and resp.json()["detail"] == "Unknown room"


def test_room_token_stays_out_of_the_environment(in_tmp, host, monkeypatch):
    """Regression: the token sat in os.environ, inherited by cloudflared and git."""
    seen = []

    def tunnel(port=0):
        token = settings(in_tmp)["CCBRIDGE_TOKEN"]
        seen.append(any(token in value for value in os.environ.values()))
        return None, None

    monkeypatch.setattr(bridge, "start_tunnel", tunnel)
    host()
    assert seen == [False]


def test_host_claims_its_own_name_first(in_tmp, host):
    """Regression: until the host's Claude Code started, anyone with the invite could take its name."""
    resp = host(while_up=lambda: join_as(in_tmp, "sourabh", "an-impostors-key-0123456789"))
    assert resp.status_code == 409


def test_host_points_claude_code_at_its_own_relay(in_tmp, host):
    status = host(while_up=lambda: httpx.get(f"{relay_of(in_tmp)[0]}/healthz").status_code)
    assert status == 200
    assert relay_of(in_tmp)[0].startswith("http://127.0.0.1:")


def test_host_keeps_its_room_until_asked_for_a_new_one(in_tmp, host, monkeypatch):
    host()
    first = settings(in_tmp)["CCBRIDGE_ROOM"]
    host()
    assert settings(in_tmp)["CCBRIDGE_ROOM"] == first
    host(new_room=True)
    assert settings(in_tmp)["CCBRIDGE_ROOM"] != first

    other = in_tmp.parent / "other-project"
    other.mkdir()
    monkeypatch.chdir(other)
    host()
    assert settings(other)["CCBRIDGE_ROOM"] not in (first, settings(in_tmp)["CCBRIDGE_ROOM"])


def test_host_carries_on_in_the_room_0_2_used(in_tmp, host, monkeypatch):
    """After an upgrade, the project that used 0.2's one room keeps it; others get their own."""
    bridge.LEGACY_TOKEN_FILE.write_text("bridge-0a1b2c3d\nlegacy-token-0123456789abc\n", encoding="utf-8")
    old = {"mcpServers": {"ccbridge": {"env": {"CCBRIDGE_ROOM": "bridge-0a1b2c3d"}}}}
    (in_tmp / ".mcp.json").write_text(json.dumps(old), encoding="utf-8")
    host()
    assert settings(in_tmp)["CCBRIDGE_ROOM"] == "bridge-0a1b2c3d"

    other = in_tmp.parent / "other-project"
    other.mkdir()
    monkeypatch.chdir(other)
    host()
    assert settings(other)["CCBRIDGE_ROOM"] != "bridge-0a1b2c3d"


def test_host_allow_locks_the_room(in_tmp, host):
    def try_names():
        return {name: join_as(in_tmp, name, f"key-of-{name}-0123456789").status_code
                for name in ("alex", "mallory")}

    assert host(allow="alex", while_up=try_names) == {"alex": 200, "mallory": 403}


def test_busy_port_stops_the_host_before_anything_starts(in_tmp, monkeypatch):
    started = []
    monkeypatch.setattr(bridge, "start_tunnel", lambda port=0: started.append(port) or (None, None))
    running = "python bridge.py join THE-RUNNING-BRIDGES-INVITE <theirname>\n"
    bridge.INVITE_FILE.write_text(running, encoding="utf-8")
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        monkeypatch.setattr(bridge, "PORT", busy.getsockname()[1])
        with pytest.raises(SetupError, match="already in use"):
            bridge.cmd_host("sourabh", allow=None)

    assert started == []
    assert not (in_tmp / ".mcp.json").exists()
    # The bridge that holds the port is still running: its invite stays.
    assert bridge.INVITE_FILE.read_text(encoding="utf-8") == running


def test_an_old_invite_is_gone_before_the_slow_start(in_tmp, host, monkeypatch):
    """Regression: an agent reading invite.txt while the tunnel started got the last run's invite."""
    bridge.INVITE_FILE.write_text("python bridge.py join STALE <theirname>\n", encoding="utf-8")
    seen = []

    def tunnel(port=0):
        seen.append(bridge.INVITE_FILE.exists())
        return None, None

    monkeypatch.setattr(bridge, "start_tunnel", tunnel)
    assert "STALE" not in host(while_up=lambda: bridge.INVITE_FILE.read_text(encoding="utf-8"))
    assert seen == [False]


def test_stopping_leaves_an_invite_written_by_another_run(in_tmp, host):
    def replaced_by_another_run():
        bridge.INVITE_FILE.write_text("another run's invite\n", encoding="utf-8")

    host(while_up=replaced_by_another_run)
    assert bridge.INVITE_FILE.read_text(encoding="utf-8") == "another run's invite\n"


# --- join ----------------------------------------------------------------------------


@pytest.fixture()
def friend_project(in_tmp, monkeypatch):
    """Switch to a second project folder, with an identity key of its own."""
    project = in_tmp.parent / "friend-project"
    project.mkdir()

    def switch():
        monkeypatch.chdir(project)
        monkeypatch.setattr(bridge, "IDENTITY_FILE", in_tmp.parent / "friend_identity")
        return project

    return switch


def invite_for(project, token=None):
    url, room, real_token = relay_of(project)
    return encode_invite(url, room, token or real_token)


def test_join_claims_the_name_on_the_relay(in_tmp, host, friend_project):
    def join_then_impersonate():
        invite = invite_for(in_tmp)
        friend = friend_project()
        bridge.cmd_join(invite, "alex", allow=None)
        assert settings(friend)["CCBRIDGE_NAME"] == "alex"
        return join_as(friend, "alex", "an-impostors-key-0123456789").status_code

    assert host(while_up=join_then_impersonate) == 409


def test_join_with_a_taken_name_fails_and_writes_nothing(in_tmp, host, friend_project):
    def join_as_the_host():
        invite = invite_for(in_tmp)
        friend = friend_project()
        with pytest.raises(SetupError, match="belongs to someone else"):
            bridge.cmd_join(invite, "sourabh", allow=None)
        return friend

    friend = host(while_up=join_as_the_host)
    assert not (friend / ".mcp.json").exists()


def test_join_with_a_stale_invite_says_so(in_tmp, host, friend_project):
    def join_with_old_token():
        invite = invite_for(in_tmp, token="a-token-from-an-earlier-room")
        friend_project()
        with pytest.raises(SetupError, match="no longer accepts this invite"):
            bridge.cmd_join(invite, "alex", allow=None)

    host(while_up=join_with_old_token)


def test_join_still_writes_settings_when_the_relay_is_unreachable(in_tmp, monkeypatch, capsys):
    def unreachable(*args, **kwargs):
        raise bridge.RelayUnreachable("ConnectError")

    monkeypatch.setattr(bridge, "register_name", unreachable)
    code = bridge.cmd_join(encode_invite("https://x", "room1", "t"), "alex", allow=None)
    assert code == bridge.EXIT_UNREACHABLE
    assert settings(in_tmp)["CCBRIDGE_NAME"] == "alex"
    assert "No bridge answered" in capsys.readouterr().out


@pytest.fixture()
def gateway():
    """A server that answers like Cloudflare does for a quick tunnel whose bridge stopped."""
    import http.server
    import threading

    answer = {"status": 530}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            # Read the request first: on Windows, closing with it unread resets the
            # connection, and the client sees a ReadError instead of this answer.
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = b"<!DOCTYPE html><html><title>Cloudflare Tunnel error</title></html>"
            self.send_response(answer["status"])
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield answer, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.mark.parametrize("status", [530, 502, 404])
def test_a_dead_tunnel_counts_as_no_bridge(in_tmp, gateway, capsys, status):
    """Regression: a dead quick tunnel's HTML error page was shown as the bridge refusing you."""
    answer, url = gateway
    answer["status"] = status
    code = bridge.cmd_join(encode_invite(url, "room1", "t"), "alex", allow=None)
    out = capsys.readouterr().out
    assert code == bridge.EXIT_UNREACHABLE
    assert f"No bridge answered at {url} (HTTP {status})" in out and "<html>" not in out


@pytest.mark.parametrize("name", ["", "has\nnewline", "<system>", "x" * 65])
def test_bad_names_are_refused(in_tmp, name):
    with pytest.raises(SetupError, match="not a usable name"):
        bridge.cmd_join(encode_invite("https://x", "room1", "t"), name, allow=None)


def test_join_cli_writes_allowlist(in_tmp, monkeypatch):
    monkeypatch.setattr(bridge, "register_name", lambda *args, **kwargs: None)
    bridge.main(["join", encode_invite("https://x", "room1", "t"), "friend", "--allow", "sourabh"])
    assert settings(in_tmp)["CCBRIDGE_ALLOWED_PEERS"] == "sourabh"


def test_join_cli_reports_bad_invite_without_a_traceback(in_tmp, capsys):
    with pytest.raises(SystemExit) as exc:
        bridge.main(["join", "garbage", "friend"])
    assert exc.value.code == 1
    assert "Error: that invite is not valid" in capsys.readouterr().err


# --- review fixes ----------------------------------------------------------------


def test_programs_in_the_project_folder_are_never_run(in_tmp, monkeypatch, tmp_path):
    """Regression: on Windows, shutil.which ran a git.cmd or cloudflared.exe from the project."""
    for name in ("git", "git.cmd", "git.exe", "cloudflared", "cloudflared.cmd", "cloudflared.exe"):
        (in_tmp / name).write_text("echo hijacked\n", encoding="utf-8")
        if POSIX:
            os.chmod(in_tmp / name, 0o755)
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join([str(empty), ".", ""]))
    assert bridge.find_program("git") is None
    assert bridge.find_program("cloudflared") is None
    assert bridge.start_tunnel() == (None, None)
    assert bridge.mcp_config_warnings() == []


def test_find_program_finds_programs_on_path(tmp_path, monkeypatch):
    folder = tmp_path / "bin"
    folder.mkdir()
    exe = folder / ("tool.exe" if os.name == "nt" else "tool")
    exe.write_text("", encoding="utf-8")
    if POSIX:
        os.chmod(exe, 0o755)
    monkeypatch.setenv("PATH", str(folder))
    found = bridge.find_program("tool")
    assert found and os.path.normcase(found) == os.path.normcase(str(exe))


def test_the_tunnel_points_at_the_address_the_relay_holds(monkeypatch):
    """Regression: cloudflared was pointed at "localhost", which may be another program on ::1."""
    started = []

    class FakeCloudflared:
        def __init__(self, args, **kwargs):
            started.append(args)
            self.stdout = iter([b"INF |  https://some-random-words.trycloudflare.com  |\n"])

        def poll(self):
            return None

    monkeypatch.setattr(bridge, "find_program", lambda name: "cloudflared")
    monkeypatch.setattr(bridge.subprocess, "Popen", FakeCloudflared)
    proc, url = bridge.start_tunnel(4321)
    assert url == "https://some-random-words.trycloudflare.com"
    assert started[0][-1] == "http://127.0.0.1:4321"


def test_host_works_behind_a_proxy(in_tmp, host, monkeypatch):
    """Regression: with HTTP_PROXY set, host sent its own loopback requests to the proxy."""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    resp = host(while_up=lambda: join_as(in_tmp, "sourabh", "an-impostors-key-0123456789"))
    assert resp.status_code == 409  # the host got up, and claimed its name


def test_0_2_room_goes_to_one_project_only(in_tmp, host, monkeypatch):
    """Regression: 0.2 used one room for every project; each of them then took it over."""
    bridge.LEGACY_TOKEN_FILE.write_text("bridge-0a1b2c3d\nlegacy-token-0123456789abc\n", encoding="utf-8")
    old = json.dumps({"mcpServers": {"ccbridge": {"env": {"CCBRIDGE_ROOM": "bridge-0a1b2c3d"}}}})
    other = in_tmp.parent / "other-project"
    other.mkdir()
    for project in (in_tmp, other):
        (project / ".mcp.json").write_text(old, encoding="utf-8")

    host()
    assert settings(in_tmp)["CCBRIDGE_ROOM"] == "bridge-0a1b2c3d"
    assert not bridge.LEGACY_TOKEN_FILE.exists()  # now in this project's settings
    monkeypatch.chdir(other)
    host()
    assert settings(other)["CCBRIDGE_ROOM"] != "bridge-0a1b2c3d"


def test_new_room_retires_the_0_2_room(in_tmp, host, monkeypatch):
    bridge.LEGACY_TOKEN_FILE.write_text("bridge-0a1b2c3d\nlegacy-token-0123456789abc\n", encoding="utf-8")
    old = json.dumps({"mcpServers": {"ccbridge": {"env": {"CCBRIDGE_ROOM": "bridge-0a1b2c3d"}}}})
    (in_tmp / ".mcp.json").write_text(old, encoding="utf-8")
    host(new_room=True)
    assert settings(in_tmp)["CCBRIDGE_ROOM"] != "bridge-0a1b2c3d"
    assert not bridge.LEGACY_TOKEN_FILE.exists()


def test_a_moved_project_keeps_its_room(in_tmp, host, monkeypatch):
    """Regression: renaming the project folder silently started a new room."""
    host()
    room, old_file = settings(in_tmp)["CCBRIDGE_ROOM"], bridge.settings_path(in_tmp)
    moved = in_tmp.parent / "renamed-project"
    monkeypatch.chdir(in_tmp.parent)
    in_tmp.rename(moved)
    monkeypatch.chdir(moved)
    host()
    assert settings(moved)["CCBRIDGE_ROOM"] == room
    assert not old_file.exists()  # moved with the project, not left behind


def test_a_copied_project_gets_its_own_room(in_tmp, host, monkeypatch):
    host()
    room = settings(in_tmp)["CCBRIDGE_ROOM"]
    copy = in_tmp.parent / "copy-of-project"
    shutil.copytree(in_tmp, copy)
    monkeypatch.chdir(copy)
    host()
    assert settings(copy)["CCBRIDGE_ROOM"] != room
    assert settings(in_tmp)["CCBRIDGE_ROOM"] == room


def test_settings_are_replaced_in_one_step(in_tmp, monkeypatch):
    """Regression: a reader could see a half-written settings file while it was rewritten."""
    import threading

    write_mcp_config("https://relay", "room-a", "tok", "me", peer=None)
    path = bridge.settings_path(in_tmp)
    monkeypatch.setenv("CCBRIDGE_CONFIG", str(path))
    done = threading.Event()
    failures = []

    def writer():
        try:
            for i in range(200):
                room = "room-a" if i % 2 else "a-much-longer-room-name-" + "x" * (i % 17)
                bridge._write_private(path, json.dumps({"CCBRIDGE_ROOM": room}) + "\n")
        except Exception as exc:  # noqa: BLE001
            failures.append(exc)
        finally:
            done.set()

    threading.Thread(target=writer).start()
    reads = 0
    while not done.is_set():
        try:
            assert config.setting("CCBRIDGE_ROOM")
        except config.ConfigError as exc:
            failures.append(exc)
        reads += 1
    assert failures == [] and reads > 0
    assert [p.name for p in path.parent.iterdir() if p.suffix == ".tmp"] == []


def test_stop_signals_run_the_cleanup():
    import signal

    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(KeyboardInterrupt):
        with bridge._stop_on_signals():
            signal.raise_signal(signal.SIGTERM)
    assert signal.getsignal(signal.SIGTERM) == before


# --- more AI tools: connect, --client, --local, --max-members ---------------------


@pytest.fixture()
def tools(in_tmp, tmp_path, monkeypatch):
    """A scratch home folder, and no Codex CLI, so nothing outside tmp_path is touched."""
    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.setenv("CODEX_HOME", "")
    monkeypatch.delenv("CODEX_HOME")
    real = bridge.find_program
    monkeypatch.setattr(bridge, "find_program", lambda name: None if name == "codex" else real(name))
    return home


def client_settings(project, client):
    return json.loads(bridge.client_settings_path(project, client).read_text(encoding="utf-8"))


def test_host_with_no_client_flag_writes_exactly_what_it_did_before(in_tmp, host, tools):
    host()
    assert (in_tmp / ".mcp.json").exists()
    assert sorted(p.name for p in bridge.PROJECTS_DIR.iterdir()) == [bridge.settings_path(in_tmp).name]
    assert not (in_tmp / ".cursor").exists()
    s = settings(in_tmp)
    assert s["CCBRIDGE_NAME"] == "sourabh" and s["CCBRIDGE_CLIENT"] == "claude-code"
    assert s["CCBRIDGE_PROJECT_DIR"] == str(in_tmp.resolve())


def test_connect_gives_every_tool_its_own_name_and_settings(in_tmp, host, tools):
    def connect_then_probe():
        assert bridge.cmd_connect("cursor,grok,antigravity", None) == 0
        key = settings(in_tmp)["CCBRIDGE_IDENTITY_KEY"]
        seen = {n: join_as(in_tmp, n, key, register_only=True).status_code
                for n in ("sourabh-cursor", "sourabh-grok", "sourabh-antigravity")}
        seen["impostor"] = join_as(in_tmp, "sourabh-cursor", "some-other-key-0123456789",
                                   register_only=True).status_code
        return seen

    # One identity key may own several names in a room; nobody else may take them.
    assert host(while_up=connect_then_probe) == {"sourabh-cursor": 200, "sourabh-grok": 200,
                                                  "sourabh-antigravity": 200, "impostor": 409}
    files = {bridge.client_settings_path(in_tmp, c) for c in ("claude-code", "cursor", "grok", "antigravity")}
    assert len(files) == 4
    for client in ("cursor", "grok", "antigravity"):
        s = client_settings(in_tmp, client)
        assert s["CCBRIDGE_NAME"] == f"sourabh-{client}" and s["CCBRIDGE_CLIENT"] == client
        assert s["CCBRIDGE_PROJECT_DIR"] == str(in_tmp.resolve())
        assert s["CCBRIDGE_ROOM"] == settings(in_tmp)["CCBRIDGE_ROOM"]
        assert "hosted" not in s
    assert settings(in_tmp)["CCBRIDGE_NAME"] == "sourabh"  # Claude Code's is unchanged


CLIENT_FILES = {"cursor": ".cursor/mcp.json", "vscode": ".vscode/mcp.json", "opencode": "opencode.jsonc",
                "kiro": ".kiro/settings/mcp.json", "gemini": ".gemini/settings.json",
                "grok": ".grok/config.toml", "codex": ".codex/config.toml",
                "antigravity": ".agents/mcp_config.json", "devin": ".devin/mcp_config.json",
                "amp": ".amp/settings.json", "junie": ".junie/mcp/mcp.json", "zed": ".zed/settings.json"}


def windsurf_file(home):
    path = home / ".codeium/windsurf/mcp_config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"mcpServers": {}}', encoding="utf-8")
    return path


def test_client_configs_hold_no_secret_and_name_their_own_settings(in_tmp, host, tools):
    windsurf_file(tools)
    host(while_up=lambda: bridge.cmd_connect(",".join(CLIENT_FILES) + ",windsurf", None))
    token, key = settings(in_tmp)["CCBRIDGE_TOKEN"], settings(in_tmp)["CCBRIDGE_IDENTITY_KEY"]
    written = {c: in_tmp / f for c, f in CLIENT_FILES.items()}
    written["windsurf"] = tools / ".codeium/windsurf/mcp_config.json"
    for client, path in written.items():
        text = path.read_text(encoding="utf-8")
        assert token not in text and key not in text, path
        own = str(bridge.client_settings_path(in_tmp, client))
        assert own in text or json.dumps(own)[1:-1] in text, path
    ignored = (in_tmp / ".gitignore").read_text(encoding="utf-8").split()
    for entry in [".mcp.json", *CLIENT_FILES.values()]:
        assert entry in ignored


def test_user_wide_entries_name_the_project(in_tmp, host, tools):
    path = windsurf_file(tools)
    host(while_up=lambda: bridge.cmd_connect("windsurf", None))
    servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
    (name,) = servers
    assert re.fullmatch(r"ccbridge_[a-z0-9]+(?:_[a-z0-9]+)*", name) and len(name) <= 29
    assert name.endswith("_" + bridge.settings_path(in_tmp).stem[-12:][:6])
    assert servers[name]["env"] == {"CCBRIDGE_CONFIG": str(bridge.client_settings_path(in_tmp, "windsurf"))}


def test_connect_name_override(in_tmp, host, tools):
    host(while_up=lambda: bridge.cmd_connect("cursor", "reviewer"))
    assert client_settings(in_tmp, "cursor")["CCBRIDGE_NAME"] == "reviewer"


def test_connect_before_host_or_join_says_what_to_do(in_tmp, tools):
    with pytest.raises(SetupError, match="not on a bridge yet"):
        bridge.cmd_connect("cursor", None)


def test_a_name_too_long_with_the_client_suffix_is_refused(in_tmp, tools):
    with pytest.raises(SetupError, match="pick a shorter one with --name"):
        bridge._client_names("x" * 60, ["antigravity"])


def test_connect_into_a_locked_room_explains_the_lock(in_tmp, host, tools):
    def connect_cursor():
        with pytest.raises(SetupError, match="--client cursor"):
            bridge.cmd_connect("cursor", None)

    host(allow="alex", while_up=connect_cursor)


def test_host_with_clients_lets_them_into_its_locked_room(in_tmp, host, tools, monkeypatch):
    monkeypatch.setattr(bridge, "serve_until_stopped", lambda thread: None)
    bridge.cmd_host("sourabh", "alex", clients=["claude-code", "cursor"])
    assert client_settings(in_tmp, "cursor")["CCBRIDGE_NAME"] == "sourabh-cursor"
    assert (in_tmp / ".mcp.json").exists() and (in_tmp / ".cursor/mcp.json").exists()


def test_with_an_allowlist_a_projects_tools_still_hear_each_other(in_tmp, host, tools, monkeypatch):
    from ccbridge import mcp_server

    monkeypatch.setattr(bridge, "serve_until_stopped", lambda thread: None)
    bridge.cmd_host("sourabh", "alex", clients=["claude-code", "cursor"])
    # A tool connected later is added to the earlier tools' lists too.
    s = settings(in_tmp)
    bridge.write_client(in_tmp, "grok", "sourabh-grok", s)
    bridge._sync_allowed_peers(in_tmp)

    def peers_of(client):
        monkeypatch.setenv("CCBRIDGE_CONFIG", str(bridge.client_settings_path(in_tmp, client)))
        return mcp_server.allowed_peers()

    assert peers_of("claude-code") == {"alex", "sourabh-cursor", "sourabh-grok"}
    assert peers_of("cursor") == {"alex", "sourabh", "sourabh-grok"}
    assert peers_of("grok") == {"alex", "sourabh", "sourabh-cursor"}


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_tracked_tool_config_is_pointed_out(in_tmp, tools):
    subprocess.run(["git", "init", "-q"], cwd=in_tmp, check=True)
    (in_tmp / "opencode.json").write_text('{"theme": "x"}', encoding="utf-8")
    subprocess.run(["git", "add", "opencode.json"], cwd=in_tmp, check=True)
    notes = bridge.client_config_warnings(in_tmp, {"opencode": in_tmp / "opencode.json",
                                                   "cursor": in_tmp / ".cursor/mcp.json"})
    assert len(notes) == 1 and notes[0].startswith("opencode.json is tracked by git")


def test_without_an_allowlist_none_is_added(in_tmp, host, tools):
    host(while_up=lambda: bridge.cmd_connect("cursor", None))
    assert "CCBRIDGE_ALLOWED_PEERS" not in client_settings(in_tmp, "cursor")
    assert "CCBRIDGE_ALLOWED_PEERS" not in settings(in_tmp)


def test_host_without_claude_code_leaves_mcp_json_alone(in_tmp, host, tools, monkeypatch):
    monkeypatch.setattr(bridge, "serve_until_stopped", lambda thread: None)
    bridge.cmd_host("sourabh", None, clients=["cursor"])
    assert not (in_tmp / ".mcp.json").exists()
    assert client_settings(in_tmp, "cursor")["CCBRIDGE_NAME"] == "sourabh-cursor"
    assert settings(in_tmp)["hosted"]  # the project's record is kept all the same


def test_join_with_clients(in_tmp, host, tools, friend_project):
    def join_with_two():
        invite = invite_for(in_tmp)
        friend = friend_project()
        assert bridge.cmd_join(invite, "alex", None, clients=["claude-code", "opencode"]) == 0
        return friend

    friend = host(while_up=join_with_two)
    assert json.loads((friend / "opencode.jsonc").read_text(encoding="utf-8"))["mcp"]["ccbridge"]["type"] == "local"
    assert client_settings(friend, "opencode")["CCBRIDGE_NAME"] == "alex-opencode"
    assert settings(friend)["CCBRIDGE_NAME"] == "alex"


def test_host_local_never_starts_a_tunnel(in_tmp, tools, monkeypatch, capsys):
    def no_tunnel(port=0):
        raise AssertionError("--local must not start cloudflared")

    monkeypatch.setattr(bridge, "start_tunnel", no_tunnel)
    monkeypatch.setattr(bridge, "serve_until_stopped", lambda thread: None)
    bridge.cmd_host("sourabh", None, local=True)
    assert "LOCAL ONLY (--local)" in capsys.readouterr().out


def test_max_members_needs_a_relay_that_supports_it(in_tmp):
    import inspect

    from ccbridge import relay

    if "max_members" in inspect.signature(relay.create_app).parameters:
        pytest.skip("this relay already supports max_members")
    with pytest.raises(SetupError, match="no member limit"):
        bridge.build_relay("room1", "t" * 20, None, max_members=5)


def test_max_members_is_passed_to_the_relay(in_tmp, monkeypatch):
    from ccbridge import relay

    seen = {}

    def create_app(db_path, open_rooms=None, rooms=None, allowed_names=None, max_members=None):
        seen["max_members"] = max_members
        return "app"

    monkeypatch.setattr(relay, "create_app", create_app)
    assert bridge.build_relay("room1", "t" * 20, None, max_members=5) == "app"
    assert seen == {"max_members": 5}


@pytest.fixture()
def links(monkeypatch):
    """A relay with join links, as on the hub branch: invite_codes and POST /v1/invite."""
    from fastapi import HTTPException

    import inspect

    from ccbridge import relay

    real = relay.create_app
    if "invite_codes" in inspect.signature(real).parameters:
        return  # the real relay has join links: test against it

    def create_app(db_path, open_rooms=None, rooms=None, allowed_names=None, invite_codes=None):
        app = real(db_path, open_rooms=open_rooms, rooms=rooms, allowed_names=allowed_names)
        codes = {k.replace("-", "").upper(): v for k, v in (invite_codes or {}).items()}

        @app.post("/v1/invite")
        def invite(body: dict):
            hit = codes.get(str(body.get("code", "")).replace("-", "").upper())
            if not hit:
                raise HTTPException(404, "Unknown join link")
            return {"room": hit[0], "token": hit[1], "version": "test"}

        return app

    monkeypatch.setattr(relay, "create_app", create_app)


def link_of():
    return bridge.INVITE_FILE.read_text(encoding="utf-8").split()[3]


def test_new_invite_code_shape():
    for _ in range(50):
        assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}", bridge.new_invite_code())


@pytest.mark.parametrize("text,expected", [
    ("https://a-b-c.trycloudflare.com/j/7Q4K-M9XP", ("https://a-b-c.trycloudflare.com", "7Q4K-M9XP")),
    ("https://a-b-c.trycloudflare.com/j/7Q4K-M9XP/", ("https://a-b-c.trycloudflare.com", "7Q4K-M9XP")),
    ("  http://127.0.0.1:8787/j/7q4km9xp\n", ("http://127.0.0.1:8787", "7q4km9xp")),
    ("https://example.com/relay/j/7Q4K-M9XP", ("https://example.com/relay", "7Q4K-M9XP")),
    ("https://example.com/j/", None),
    ("https://example.com/j/7Q4K-M9XP?x=1", None),
    (bridge.encode_invite("https://x", "room1", "tok"), None),
])
def test_parse_join_link(text, expected):
    assert bridge.parse_join_link(text) == expected


def test_host_prints_a_join_link_and_keeps_it(in_tmp, host, links, capsys):
    first = host(while_up=link_of)
    assert not bridge.INVITE_FILE.exists()  # removed when the bridge stops
    url = relay_of(in_tmp)[0]
    assert re.fullmatch(re.escape(url) + r"/j/[0-9A-Z]{4}-[0-9A-Z]{4}", first)
    assert f"Join link: {first}" in capsys.readouterr().out
    code = first.rsplit("/", 1)[1]
    assert host(while_up=link_of).rsplit("/", 1)[1] == code  # the same code next run
    assert host(new_room=True, while_up=link_of).rsplit("/", 1)[1] != code


def test_invite_file_has_the_link_first_and_the_full_invite_last(in_tmp, host, links):
    text = host(while_up=lambda: bridge.INVITE_FILE.read_text(encoding="utf-8"))
    first, last = text.splitlines()
    assert "/j/" in first and first.endswith("<theirname>")
    assert bridge.decode_invite(last.split()[3])[1] == settings(in_tmp)["CCBRIDGE_ROOM"]


def test_join_with_a_link(in_tmp, host, links, friend_project):
    def join_by_link():
        link = link_of() + "/"
        friend = friend_project()
        assert bridge.cmd_join(link, "alex", None) == 0
        return friend

    friend = host(while_up=join_by_link)
    assert settings(friend)["CCBRIDGE_ROOM"] == settings(in_tmp)["CCBRIDGE_ROOM"]
    assert settings(friend)["CCBRIDGE_NAME"] == "alex"


def test_join_with_an_unknown_link_says_so(in_tmp, host, links, friend_project):
    def join_with_wrong_code():
        base = link_of().rsplit("/", 2)[0]
        friend = friend_project()
        with pytest.raises(SetupError, match="join link is not valid"):
            bridge.cmd_join(f"{base}/j/ZZZZ-ZZZZ", "alex", None)
        return friend

    friend = host(while_up=join_with_wrong_code)
    assert not (friend / ".mcp.json").exists()


def test_join_with_a_link_when_no_bridge_answers(in_tmp, capsys):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed again before the join
    code = bridge.cmd_join(f"http://127.0.0.1:{port}/j/7Q4K-M9XP", "alex", None)
    assert code == bridge.EXIT_UNREACHABLE
    assert "Nothing was saved" in capsys.readouterr().out
    assert not (in_tmp / ".mcp.json").exists()


def test_no_link_from_a_relay_without_join_links(in_tmp, host):
    if bridge._relay_accepts("invite_codes"):
        pytest.skip("this relay has join links")
    text = host(while_up=lambda: bridge.INVITE_FILE.read_text(encoding="utf-8"))
    assert len(text.splitlines()) == 1 and "/j/" not in text


def test_host_limits_members_to_32_by_default(in_tmp, host, monkeypatch):
    from ccbridge import relay

    real, seen = relay.create_app, {}

    def create_app(db_path, open_rooms=None, rooms=None, allowed_names=None, max_members=None):
        seen["max_members"] = max_members
        return real(db_path, open_rooms=open_rooms, rooms=rooms, allowed_names=allowed_names)

    monkeypatch.setattr(relay, "create_app", create_app)
    host()
    assert seen == {"max_members": 32}
    monkeypatch.setattr(bridge, "serve_until_stopped", lambda thread: None)
    bridge.cmd_host("sourabh", None, max_members=500)
    assert seen == {"max_members": 500}


def test_clients_command_lists_every_client(in_tmp, tools, capsys):
    bridge.main(["clients"])
    out = capsys.readouterr().out
    for slug in bridge.CLIENTS:
        assert slug in out


def test_unknown_client_is_a_friendly_error(in_tmp, tools, capsys):
    with pytest.raises(SystemExit) as exit_:
        bridge.main(["connect", "--client", "emacs"])
    assert exit_.value.code == 1
    assert "unknown client 'emacs'" in capsys.readouterr().err
