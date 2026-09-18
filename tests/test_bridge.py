import json
import os

import pytest

from bridge import decode_invite, encode_invite, write_mcp_config


def test_invite_round_trip():
    url, room, token = "https://x.trycloudflare.com", "bridge-ab12", "tok_123-_"
    assert decode_invite(encode_invite(url, room, token)) == (url, room, token)


def test_invite_survives_whitespace_from_copy_paste():
    invite = encode_invite("https://x.trycloudflare.com", "room", "tok")
    assert decode_invite(f"  {invite}\n") == ("https://x.trycloudflare.com", "room", "tok")


@pytest.fixture()
def in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_write_mcp_config_shape(in_tmp):
    write_mcp_config("https://relay", "room1", "tok", "friend", peer="sourabh")
    cfg = json.loads((in_tmp / ".mcp.json").read_text())

    server = cfg["mcpServers"]["ccbridge"]
    assert server["args"] == ["-m", "ccbridge.mcp_server"]
    assert server["env"]["CCBRIDGE_RELAY_URL"] == "https://relay"
    assert server["env"]["CCBRIDGE_NAME"] == "friend"
    assert server["env"]["CCBRIDGE_ALLOWED_PEERS"] == "sourabh"
    assert os.path.isdir(server["env"]["PYTHONPATH"])


def test_token_file_is_gitignored(in_tmp):
    write_mcp_config("https://relay", "room1", "tok", "me", peer=None)
    assert ".mcp.json" in (in_tmp / ".gitignore").read_text()


def test_existing_mcp_servers_are_preserved(in_tmp):
    (in_tmp / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    write_mcp_config("https://relay", "room1", "tok", "me", peer=None)

    cfg = json.loads((in_tmp / ".mcp.json").read_text())
    assert "other" in cfg["mcpServers"]
    assert "ccbridge" in cfg["mcpServers"]


def test_existing_gitignore_is_appended_not_replaced(in_tmp):
    (in_tmp / ".gitignore").write_text("*.pyc\n")
    write_mcp_config("https://relay", "room1", "tok", "me", peer=None)

    content = (in_tmp / ".gitignore").read_text()
    assert "*.pyc" in content and ".mcp.json" in content
