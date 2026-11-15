import json
import re
import sys

import pytest

from ccbridge.clients import (CLIENTS, STYLES, TOML_EXTRA, ClientError, config_path,
                              parse_clients, register, server_name)

CMD, ARGS = "C:/Python/python.exe", ["C:/ccbridge/run_mcp_server.py"]
ENV = {"CCBRIDGE_CONFIG": "C:/ccbridge/.ccbridge_projects/p-1234-cursor.json"}
JSON_CLIENTS = [s for s, c in CLIENTS.items() if c.style in STYLES and not c.only_if_exists]
TOML_CLIENTS = [s for s, c in CLIENTS.items() if c.style in TOML_EXTRA]
tomllib = pytest.importorskip("tomllib") if sys.version_info >= (3, 11) else None


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A scratch home folder, so no test touches the real user-wide configs."""
    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    return home


@pytest.fixture()
def project(tmp_path, home):
    project = tmp_path / "project"
    project.mkdir()
    return project


def no_programs(name):
    return None


def put(client, project, name="ccbridge", env=ENV):
    return register(CLIENTS[client], project, name, CMD, ARGS, env)[0]


def entries(client, path):
    """The servers in ``client``'s file, as {name: (command, args, env)}."""
    spec = CLIENTS[client]
    text = path.read_text(encoding="utf-8")
    if spec.style in TOML_EXTRA:
        if not tomllib:
            return None
        data = tomllib.loads(text)["mcp_servers"]
        return {k: (v["command"], v["args"], v["env"]) for k, v in data.items()}
    key, env_key = STYLES[spec.style]
    data = json.loads(text)[key]
    if spec.style == "opencode":
        return {k: (v["command"][0], v["command"][1:], v[env_key]) for k, v in data.items()}
    return {k: (v["command"], v["args"], v[env_key]) for k, v in data.items()}


def make(client, project, text):
    path = config_path(CLIENTS[client], project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text if isinstance(text, bytes) else text.encode())
    return path


# --- the table -------------------------------------------------------------------


def test_every_client_has_a_known_style_and_a_path_template():
    for slug, spec in CLIENTS.items():
        assert spec.slug == slug
        assert spec.style in STYLES or spec.style in TOML_EXTRA
        assert all(t.startswith(("{project}/", "~/")) for t in spec.path.split("|"))
        assert spec.source in ("tested", "docs")


def test_only_windsurf_is_user_wide():
    # A user-wide entry starts in every project's window: two windows, one name.
    assert [s for s, c in CLIENTS.items() if c.user_wide] == ["windsurf"]


def test_paths_resolve_at_call_time(project, home):
    assert config_path(CLIENTS["cursor"], project) == project / ".cursor" / "mcp.json"
    assert config_path(CLIENTS["antigravity"], project) == project / ".agents" / "mcp_config.json"
    assert config_path(CLIENTS["codex"], project) == project / ".codex" / "config.toml"
    assert config_path(CLIENTS["windsurf"], project) == home / ".codeium" / "windsurf" / "mcp_config.json"


def test_opencode_uses_the_file_that_exists(project):
    assert config_path(CLIENTS["opencode"], project) == project / "opencode.jsonc"
    (project / "opencode.json").write_text("{}", encoding="utf-8")
    assert config_path(CLIENTS["opencode"], project) == project / "opencode.json"


def test_parse_clients(home):
    assert parse_clients(None, no_programs) == ["claude-code"]
    assert parse_clients("Cursor, codex,cursor", no_programs) == ["cursor", "codex"]
    assert parse_clients("all-installed", lambda p: p if p in ("grok", "code") else None) == ["grok", "vscode"]
    with pytest.raises(ClientError, match="unknown client 'emacs'"):
        parse_clients("emacs", no_programs)
    with pytest.raises(ClientError, match="none of the supported clients"):
        parse_clients("all-installed", no_programs)


def test_installed_is_also_seen_from_the_tools_own_folder(home):
    (home / ".gemini" / "antigravity").mkdir(parents=True)
    assert "antigravity" in parse_clients("all-installed", no_programs)


def test_all_installed_prefers_devin_over_old_windsurf(home, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (home / ".codeium" / "windsurf").mkdir(parents=True)
    (home / ".codeium" / "windsurf" / "mcp_config.json").write_text("{}", encoding="utf-8")
    assert "windsurf" in parse_clients("all-installed", no_programs)
    (home / ".config" / "devin").mkdir(parents=True)
    found = parse_clients("all-installed", no_programs)
    assert "devin" in found and "windsurf" not in found


def test_server_names_are_portable():
    assert server_name(CLIENTS["cursor"], "my.proj-ab12cd34ef56") == "ccbridge"
    for key in ("my.proj-ab12cd34ef56", "A Very Long Project Name Indeed-0123456789ab", "x-ffffffffffff"):
        name = server_name(CLIENTS["windsurf"], key)
        assert re.fullmatch(r"ccbridge_[a-z0-9]+(?:_[a-z0-9]+)*", name), name
        assert len(name) <= 29 and "__" not in name
    assert server_name(CLIENTS["windsurf"], "my.proj-ab12cd34ef56") == "ccbridge_my_proj_ab12cd"


# --- JSON configs ----------------------------------------------------------------


@pytest.mark.parametrize("client", JSON_CLIENTS)
def test_json_config_is_created(client, project):
    path = put(client, project)
    assert entries(client, path) == {"ccbridge": (CMD, ARGS, ENV)}
    assert path.read_bytes().endswith(b"}\n")


@pytest.mark.parametrize("client", JSON_CLIENTS)
def test_json_config_keeps_other_servers_and_settings(client, project):
    key = STYLES[CLIENTS[client].style][0]
    path = make(client, project, json.dumps({"theme": "dark", key: {"other": {"command": "x"}}}))
    put(client, project)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["theme"] == "dark" and data[key]["other"] == {"command": "x"}
    assert "ccbridge" in data[key]


@pytest.mark.parametrize("client", JSON_CLIENTS)
def test_empty_json_config_is_filled_in(client, project):
    make(client, project, b"")  # Antigravity ships its mcp_config.json like this
    assert entries(client, put(client, project)) == {"ccbridge": (CMD, ARGS, ENV)}


@pytest.mark.parametrize("client", JSON_CLIENTS)
def test_json_config_keeps_crlf_and_drops_a_bom(client, project):
    path = make(client, project, b"\xef\xbb\xbf{\r\n}\r\n")
    put(client, project)
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


@pytest.mark.parametrize("client", JSON_CLIENTS)
def test_json_with_comments_is_left_alone(client, project):
    original = b'{\n  // my servers\n  "x": 1\n}\n'
    path = make(client, project, original)
    with pytest.raises(ClientError, match="not plain JSON"):
        put(client, project)
    assert path.read_bytes() == original


@pytest.mark.parametrize("client", list(CLIENTS))
def test_ccbridges_tools_are_never_pre_approved(client, project, home):
    if CLIENTS[client].only_if_exists:
        make(client, project, "{}")
    text = put(client, project).read_text(encoding="utf-8").lower()
    for word in ("autoapprove", "alwaysallow", "trust", "approval", "disabled"):
        assert word not in text, word


def test_entry_shapes():
    assert put_shape("cursor") == {"type": "stdio", "command": CMD, "args": ARGS, "env": ENV}
    assert put_shape("vscode") == {"type": "stdio", "command": CMD, "args": ARGS, "env": ENV}
    assert put_shape("opencode") == {"type": "local", "command": [CMD, *ARGS], "environment": ENV,
                                     "enabled": True}
    assert put_shape("kiro") == {"command": CMD, "args": ARGS, "env": ENV}


def put_shape(client):
    from ccbridge.clients import server_entry

    return server_entry(CLIENTS[client].style, CMD, ARGS, ENV)


def test_amp_keeps_its_dotted_key(project):
    path = put("amp", project)
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {"amp.mcpServers"}


def test_rerunning_replaces_our_entry_and_keeps_the_users_env(project):
    path = put("cursor", project)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["mcpServers"]["ccbridge"]["env"]["CCBRIDGE_CONFIRM_SENDS"] = "1"
    data["mcpServers"]["ccbridge"]["env"]["CCBRIDGE_TOKEN"] = "old secret"
    path.write_text(json.dumps(data), encoding="utf-8")
    put("cursor", project, env={"CCBRIDGE_CONFIG": "new.json"})
    env = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["ccbridge"]["env"]
    assert env == {"CCBRIDGE_CONFIRM_SENDS": "1", "CCBRIDGE_CONFIG": "new.json"}


def test_old_windsurf_file_is_only_added_to_never_created(project, home):
    with pytest.raises(ClientError, match="does not exist"):
        put("windsurf", project, name="ccbridge_p")
    assert not (home / ".codeium").exists()


def test_user_wide_json_is_backed_up_first(project, home):
    path = make("windsurf", project, '{"mcpServers": {"mine": {"command": "y"}}}')
    _, copy = register(CLIENTS["windsurf"], project, "ccbridge_p", CMD, ARGS, ENV)
    assert copy.read_text(encoding="utf-8") == '{"mcpServers": {"mine": {"command": "y"}}}'
    assert set(json.loads(path.read_text(encoding="utf-8"))["mcpServers"]) == {"mine", "ccbridge_p"}


def test_project_configs_are_not_backed_up(project):
    put("cursor", project)
    _, copy = register(CLIENTS["cursor"], project, "ccbridge", CMD, ARGS, ENV)
    assert copy is None
    assert not list(project.rglob("*.ccbridge-backup"))


# --- TOML configs (Codex, Grok) ----------------------------------------------------

CODEX_CONFIG = """\
model = "gpt-5"   # keep this comment

[mcp_servers.playwright]
command = "npx"
args = ["@playwright/mcp@latest"]

[mcp_servers.node_repl.env]
NODE_OPTIONS = "--max-old-space-size=4096"
"""


@pytest.mark.parametrize("client", TOML_CLIENTS)
def test_toml_block_is_appended_and_the_rest_is_untouched(client, project):
    path = make(client, project, CODEX_CONFIG)
    put(client, project)
    text = path.read_text(encoding="utf-8")
    assert text.startswith(CODEX_CONFIG)
    assert "# >>> ccbridge ccbridge >>>" in text and "# <<< ccbridge ccbridge <<<" in text
    if tomllib:
        data = tomllib.loads(text)
        assert data["model"] == "gpt-5"
        ours = data["mcp_servers"]["ccbridge"]
        assert (ours["command"], ours["args"], ours["env"]) == (CMD, ARGS, ENV)
        assert set(data["mcp_servers"]) == {"playwright", "node_repl", "ccbridge"}


def test_codex_gets_a_longer_tool_timeout(project):
    text = put("codex", project).read_text(encoding="utf-8")
    assert "tool_timeout_sec = 120" in text
    assert "tool_timeout_sec" not in put("grok", project).read_text(encoding="utf-8")


@pytest.mark.parametrize("client", TOML_CLIENTS)
def test_toml_rerun_replaces_only_our_own_block(client, project):
    path = make(client, project, CODEX_CONFIG)
    put(client, project)
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write('\n[profiles.fast]\nmodel = "gpt-5-mini"\n')  # the user adds more later
    put(client, project, env={"CCBRIDGE_CONFIG": "D:/new.json"})
    text = path.read_text(encoding="utf-8")
    assert text.count("# >>> ccbridge ccbridge >>>") == 1
    assert "D:/new.json" in text and ENV["CCBRIDGE_CONFIG"] not in text
    assert text.startswith(CODEX_CONFIG) and text.endswith('[profiles.fast]\nmodel = "gpt-5-mini"\n')


def test_two_blocks_live_side_by_side(project):
    put("codex", project, name="ccbridge_a")
    put("codex", project, name="ccbridge_b", env={"CCBRIDGE_CONFIG": "b.json"})
    text = config_path(CLIENTS["codex"], project).read_text(encoding="utf-8")
    assert text.count("# >>> ccbridge ccbridge_a >>>") == 1 and text.count("# >>> ccbridge ccbridge_b >>>") == 1
    if tomllib:
        assert set(tomllib.loads(text)["mcp_servers"]) == {"ccbridge_a", "ccbridge_b"}


@pytest.mark.parametrize("client", TOML_CLIENTS)
def test_toml_keeps_crlf(client, project):
    path = make(client, project, CODEX_CONFIG.replace("\n", "\r\n"))
    put(client, project)
    raw = path.read_bytes()
    assert b"\n" not in raw.replace(b"\r\n", b"")


@pytest.mark.parametrize("client", TOML_CLIENTS)
def test_a_table_we_did_not_write_is_never_duplicated(client, project):
    original = '[mcp_servers.ccbridge]\ncommand = "mine"\n'
    path = make(client, project, original)
    with pytest.raises(ClientError, match="did not write"):
        put(client, project)
    assert path.read_text(encoding="utf-8") == original


def test_toml_strings_survive_quotes_and_backslashes(project):
    odd = "C:\\Users\\O'Brien\\.ccbridge_projects\\p.json"
    path = put("codex", project, env={"CCBRIDGE_CONFIG": odd})
    if tomllib:
        assert tomllib.loads(path.read_text(encoding="utf-8"))["mcp_servers"]["ccbridge"]["env"] == {
            "CCBRIDGE_CONFIG": odd}
