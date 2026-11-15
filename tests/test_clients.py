import json
import subprocess
import sys

import pytest

from ccbridge import clients
from ccbridge.clients import CLIENTS, ClientError, config_path, parse_clients, register, server_name

CMD, ARGS = "C:/Python/python.exe", ["C:/ccbridge/run_mcp_server.py"]
ENV = {"CCBRIDGE_CONFIG": "C:/ccbridge/.ccbridge_projects/p-1234-cursor.json"}
JSON_CLIENTS = [s for s, c in CLIENTS.items() if c.style != "toml"]
TOML_CLIENTS = [s for s, c in CLIENTS.items() if c.style == "toml"]
tomllib = pytest.importorskip("tomllib") if sys.version_info >= (3, 11) else None


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A scratch home folder, so no test touches the real user-wide configs."""
    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    for var in ("APPDATA", "XDG_CONFIG_HOME", "CODEX_HOME"):
        monkeypatch.setenv(var, str(home / var.lower()))
        monkeypatch.delenv(var)
    return home


@pytest.fixture()
def project(tmp_path, home):
    project = tmp_path / "project"
    project.mkdir()
    return project


def no_programs(name):
    return None


def put(client, project, name="ccbridge", env=ENV):
    return register(CLIENTS[client], project, name, CMD, ARGS, env, no_programs)[0]


def entries(client, path):
    """The ccbridge-relevant servers in ``client``'s file, as {name: (command, args, env)}."""
    spec = CLIENTS[client]
    text = path.read_text(encoding="utf-8")
    if spec.style == "toml":
        data = tomllib.loads(text)["mcp_servers"] if tomllib else None
        return {k: (v["command"], v["args"], v["env"]) for k, v in data.items()} if data else None
    data = json.loads(text)
    if spec.style == "vscode":
        return {k: (v["command"], v["args"], v["env"]) for k, v in data["servers"].items()}
    if spec.style == "opencode":
        return {k: (v["command"][0], v["command"][1:], v["environment"]) for k, v in data["mcp"].items()}
    return {k: (v["command"], v["args"], v["env"]) for k, v in data["mcpServers"].items()}


# --- the table -------------------------------------------------------------------


def test_every_client_has_a_known_style_and_a_path_template():
    for slug, spec in CLIENTS.items():
        assert spec.slug == slug
        assert spec.style in ("mcpServers", "vscode", "opencode", "toml")
        assert spec.path.startswith(("{project}/", "~/", "{codex_home}/"))
        assert spec.source in ("tested", "docs")


def test_paths_resolve_at_call_time(project, home, monkeypatch):
    assert config_path(CLIENTS["cursor"], project) == project / ".cursor" / "mcp.json"
    assert config_path(CLIENTS["windsurf"], project) == home / ".codeium" / "windsurf" / "mcp_config.json"
    assert config_path(CLIENTS["codex"], project) == home / ".codex" / "config.toml"
    monkeypatch.setenv("CODEX_HOME", str(home / "elsewhere"))
    assert config_path(CLIENTS["codex"], project) == home / "elsewhere" / "config.toml"


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


def test_user_wide_entries_are_named_per_project():
    assert server_name(CLIENTS["cursor"], "my.proj-ab12") == "ccbridge"
    # A '.' would make a dotted TOML key, i.e. a nested table.
    assert server_name(CLIENTS["codex"], "my.proj-ab12") == "ccbridge-my-proj-ab12"
    assert server_name(CLIENTS["windsurf"], "x y-1") == "ccbridge-x-y-1"


# --- JSON configs ----------------------------------------------------------------


@pytest.mark.parametrize("client", JSON_CLIENTS)
def test_json_config_is_created(client, project):
    path = put(client, project)
    assert entries(client, path) == {"ccbridge": (CMD, ARGS, ENV)}
    assert path.read_bytes().endswith(b"}\n")


@pytest.mark.parametrize("client", JSON_CLIENTS)
def test_json_config_keeps_other_servers_and_settings(client, project):
    spec = CLIENTS[client]
    path = config_path(spec, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = {"mcpServers": "mcpServers", "vscode": "servers", "opencode": "mcp"}[spec.style]
    path.write_text(json.dumps({"theme": "dark", key: {"other": {"command": "x"}}}), encoding="utf-8")
    put(client, project)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["theme"] == "dark" and data[key]["other"] == {"command": "x"}
    assert "ccbridge" in data[key]


@pytest.mark.parametrize("client", JSON_CLIENTS)
def test_empty_json_config_is_filled_in(client, project):
    path = config_path(CLIENTS[client], project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")  # Antigravity ships its mcp_config.json like this
    assert entries(client, put(client, project)) == {"ccbridge": (CMD, ARGS, ENV)}


@pytest.mark.parametrize("client", JSON_CLIENTS)
def test_json_config_keeps_crlf_and_drops_a_bom(client, project):
    path = config_path(CLIENTS[client], project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xef\xbb\xbf{\r\n}\r\n")
    put(client, project)
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


@pytest.mark.parametrize("client", JSON_CLIENTS)
def test_json_with_comments_is_left_alone(client, project):
    path = config_path(CLIENTS[client], project)
    path.parent.mkdir(parents=True, exist_ok=True)
    original = b'{\n  // my servers\n  "x": 1\n}\n'
    path.write_bytes(original)
    with pytest.raises(ClientError, match="not plain JSON"):
        put(client, project)
    assert path.read_bytes() == original


def test_rerunning_replaces_our_entry_and_keeps_the_users_env(project):
    path = put("cursor", project)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["mcpServers"]["ccbridge"]["env"]["CCBRIDGE_CONFIRM_SENDS"] = "1"
    data["mcpServers"]["ccbridge"]["env"]["CCBRIDGE_TOKEN"] = "old secret"
    path.write_text(json.dumps(data), encoding="utf-8")
    put("cursor", project, env={"CCBRIDGE_CONFIG": "new.json"})
    env = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["ccbridge"]["env"]
    assert env == {"CCBRIDGE_CONFIRM_SENDS": "1", "CCBRIDGE_CONFIG": "new.json"}


def test_user_wide_json_is_backed_up_first(project, home):
    path = config_path(CLIENTS["windsurf"], project)
    path.parent.mkdir(parents=True)
    path.write_text('{"mcpServers": {"mine": {"command": "y"}}}', encoding="utf-8")
    _, copy = register(CLIENTS["windsurf"], project, "ccbridge-p", CMD, ARGS, ENV, no_programs)
    assert copy.read_text(encoding="utf-8") == '{"mcpServers": {"mine": {"command": "y"}}}'
    assert set(json.loads(path.read_text(encoding="utf-8"))["mcpServers"]) == {"mine", "ccbridge-p"}


def test_project_configs_are_not_backed_up(project):
    put("cursor", project)
    _, copy = register(CLIENTS["cursor"], project, "ccbridge", CMD, ARGS, ENV, no_programs)
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
    path = config_path(CLIENTS[client], project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CODEX_CONFIG, encoding="utf-8", newline="")
    put(client, project, name="ccbridge-p")
    text = path.read_text(encoding="utf-8")
    assert text.startswith(CODEX_CONFIG)
    assert "# >>> ccbridge ccbridge-p >>>" in text and "# <<< ccbridge ccbridge-p <<<" in text
    if tomllib:
        data = tomllib.loads(text)
        assert data["model"] == "gpt-5"
        assert data["mcp_servers"]["ccbridge-p"] == {"command": CMD, "args": ARGS, "env": ENV}
        assert set(data["mcp_servers"]) == {"playwright", "node_repl", "ccbridge-p"}


@pytest.mark.parametrize("client", TOML_CLIENTS)
def test_toml_rerun_replaces_only_our_own_block(client, project):
    path = config_path(CLIENTS[client], project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CODEX_CONFIG, encoding="utf-8", newline="")
    put(client, project, name="ccbridge-p")
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write('\n[profiles.fast]\nmodel = "gpt-5-mini"\n')  # the user adds more later
    put(client, project, name="ccbridge-p", env={"CCBRIDGE_CONFIG": "D:/new.json"})
    text = path.read_text(encoding="utf-8")
    assert text.count("# >>> ccbridge ccbridge-p >>>") == 1
    assert "D:/new.json" in text and ENV["CCBRIDGE_CONFIG"] not in text
    assert text.startswith(CODEX_CONFIG) and text.endswith('[profiles.fast]\nmodel = "gpt-5-mini"\n')


def test_two_projects_get_two_codex_blocks(project):
    put("codex", project, name="ccbridge-a")
    put("codex", project, name="ccbridge-b", env={"CCBRIDGE_CONFIG": "b.json"})
    text = config_path(CLIENTS["codex"], project).read_text(encoding="utf-8")
    assert text.count("# >>> ccbridge ccbridge-a >>>") == 1 and text.count("# >>> ccbridge ccbridge-b >>>") == 1
    if tomllib:
        assert set(tomllib.loads(text)["mcp_servers"]) == {"ccbridge-a", "ccbridge-b"}


@pytest.mark.parametrize("client", TOML_CLIENTS)
def test_toml_keeps_crlf(client, project):
    path = config_path(CLIENTS[client], project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(CODEX_CONFIG.replace("\n", "\r\n").encode())
    put(client, project, name="ccbridge-p")
    raw = path.read_bytes()
    assert b"\n" not in raw.replace(b"\r\n", b"")


@pytest.mark.parametrize("client", TOML_CLIENTS)
def test_a_table_we_did_not_write_is_never_duplicated(client, project):
    path = config_path(CLIENTS[client], project)
    path.parent.mkdir(parents=True, exist_ok=True)
    original = '[mcp_servers.ccbridge]\ncommand = "mine"\n'
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ClientError, match="did not write"):
        put(client, project, name="ccbridge")
    assert path.read_text(encoding="utf-8") == original


def test_codex_user_config_is_backed_up_before_it_changes(project):
    path = config_path(CLIENTS["codex"], project)
    path.parent.mkdir(parents=True)
    path.write_text(CODEX_CONFIG, encoding="utf-8", newline="")
    _, copy = register(CLIENTS["codex"], project, "ccbridge-p", CMD, ARGS, ENV, no_programs)
    assert copy.read_text(encoding="utf-8") == CODEX_CONFIG


def test_toml_strings_survive_quotes_and_backslashes(project):
    odd = "C:\\Users\\O'Brien\\.ccbridge_projects\\p.json"
    path = put("codex", project, name="ccbridge-p", env={"CCBRIDGE_CONFIG": odd})
    if tomllib:
        assert tomllib.loads(path.read_text(encoding="utf-8"))["mcp_servers"]["ccbridge-p"]["env"] == {
            "CCBRIDGE_CONFIG": odd}


def test_codex_cli_is_used_when_installed(project, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(clients.subprocess, "run", fake_run)
    which = {"codex": "C:/bin/codex.exe"}.get
    register(CLIENTS["codex"], project, "ccbridge-p", CMD, ARGS, ENV, which)
    assert calls == [
        ["C:/bin/codex.exe", "mcp", "remove", "ccbridge-p"],
        ["C:/bin/codex.exe", "mcp", "add", "ccbridge-p", "--env",
         f"CCBRIDGE_CONFIG={ENV['CCBRIDGE_CONFIG']}", "--", CMD, *ARGS],
    ]
    assert not config_path(CLIENTS["codex"], project).exists()  # the CLI writes it, not us
