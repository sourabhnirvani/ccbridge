import json
import os
import sys
import types

import pytest

import bridge
from bridge import SetupError, decode_invite, encode_invite, write_mcp_config


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
    monkeypatch.setattr(bridge, "TOKEN_FILE", home / ".ccbridge_token")
    monkeypatch.setattr(bridge, "INVITE_FILE", home / "invite.txt")
    # cmd_host configures the relay through the environment; undo that afterwards.
    # (delenv alone does not record a variable that was never set, so set it first.)
    for var in ("CCBRIDGE_DB", "CCBRIDGE_ROOMS", "CCBRIDGE_OPEN_ROOMS"):
        monkeypatch.setenv(var, "")
        monkeypatch.delenv(var)
    return project


@pytest.fixture()
def no_network(monkeypatch):
    """Let cmd_host run to completion without a tunnel or a real relay."""
    monkeypatch.setattr(bridge, "start_tunnel", lambda: (None, None))
    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(run=lambda *a, **k: None))


def test_write_mcp_config_shape(in_tmp):
    write_mcp_config("https://relay", "room1", "tok", "friend", peer="sourabh")
    cfg = json.loads((in_tmp / ".mcp.json").read_text(encoding="utf-8"))

    server = cfg["mcpServers"]["ccbridge"]
    assert server["args"] == ["-m", "ccbridge.mcp_server"]
    assert server["env"]["CCBRIDGE_RELAY_URL"] == "https://relay"
    assert server["env"]["CCBRIDGE_NAME"] == "friend"
    assert server["env"]["CCBRIDGE_ALLOWED_PEERS"] == "sourabh"
    assert len(server["env"]["CCBRIDGE_IDENTITY_KEY"]) >= 16
    assert os.path.isdir(server["env"]["PYTHONPATH"])


def test_identity_key_is_stable_across_invites(in_tmp):
    """A fresh invite (new tunnel URL) must not cost you your display name."""
    write_mcp_config("https://one", "room1", "tok", "me", peer=None)
    first = json.loads((in_tmp / ".mcp.json").read_text(encoding="utf-8"))
    write_mcp_config("https://two", "room1", "tok", "me", peer=None)
    second = json.loads((in_tmp / ".mcp.json").read_text(encoding="utf-8"))
    key = "CCBRIDGE_IDENTITY_KEY"
    assert first["mcpServers"]["ccbridge"]["env"][key] == second["mcpServers"]["ccbridge"]["env"][key]


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


def test_existing_mcp_servers_are_preserved(in_tmp):
    (in_tmp / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}),
                                      encoding="utf-8")
    write_mcp_config("https://relay", "room1", "tok", "me", peer=None)

    cfg = json.loads((in_tmp / ".mcp.json").read_text(encoding="utf-8"))
    assert "other" in cfg["mcpServers"]
    assert "ccbridge" in cfg["mcpServers"]


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


def test_existing_gitignore_is_appended_not_replaced(in_tmp):
    (in_tmp / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    write_mcp_config("https://relay", "room1", "tok", "me", peer=None)

    content = (in_tmp / ".gitignore").read_text(encoding="utf-8")
    assert "*.pyc" in content and ".mcp.json" in content


def test_invite_file_is_not_written_into_the_project(in_tmp, no_network):
    """Regression: invite.txt (which holds the token) used to land, un-ignored, in the project."""
    bridge.cmd_host("sourabh", allow=None)

    assert not (in_tmp / "invite.txt").exists()
    assert bridge.INVITE_FILE.exists()
    assert "python bridge.py join " in bridge.INVITE_FILE.read_text(encoding="utf-8")


def test_repo_gitignore_covers_every_secret_file():
    lines = (bridge.HERE / ".gitignore").read_text(encoding="utf-8").splitlines()
    for secret in (".ccbridge_token", ".ccbridge_identity", "invite.txt", ".mcp.json"):
        assert secret in lines


def test_host_locks_the_relay_to_its_own_room(in_tmp, no_network):
    bridge.cmd_host("sourabh", allow=None)

    room, token = bridge.TOKEN_FILE.read_text(encoding="utf-8").split()
    assert os.environ["CCBRIDGE_ROOMS"] == f"{room}:{token}"
    assert os.environ["CCBRIDGE_OPEN_ROOMS"] == "0"


@pytest.mark.parametrize("name", ["", "has\nnewline", "<system>", "x" * 65])
def test_bad_names_are_refused(in_tmp, name):
    with pytest.raises(SetupError, match="not a usable name"):
        bridge.cmd_join(encode_invite("https://x", "room1", "t"), name, allow=None)


def test_join_cli_writes_allowlist(in_tmp):
    bridge.main(["join", encode_invite("https://x", "room1", "t"), "friend", "--allow", "sourabh"])
    cfg = json.loads((in_tmp / ".mcp.json").read_text(encoding="utf-8"))
    assert cfg["mcpServers"]["ccbridge"]["env"]["CCBRIDGE_ALLOWED_PEERS"] == "sourabh"


def test_join_cli_reports_bad_invite_without_a_traceback(in_tmp, capsys):
    with pytest.raises(SystemExit) as exc:
        bridge.main(["join", "garbage", "friend"])
    assert exc.value.code == 1
    assert "Error: that invite is not valid" in capsys.readouterr().err
