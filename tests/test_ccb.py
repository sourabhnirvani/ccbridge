"""ccbridge 2: a real hub on a free port, driven through the same commands agents run."""

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import ccb

CODE = "ABCD-EFGH-JKLM"


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("CCB_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CCB_NO_LOCALIZE", raising=False)
    h = ccb.Hub("s-test", CODE, "admin-secret")
    host = h.add_member("claude", "claude", role="host")
    srv = ccb.open_server(h, 0)
    h.on_stop = lambda: None
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield SimpleNamespace(hub=h, base=base, host_key=f"{base}/m/{host['token']}", host_token=host["token"],
                          link=f"{base}/j/{CODE}")
    h.stop()
    srv.shutdown()
    srv.server_close()


def run(capsys, *args):
    code = ccb.main([str(a) for a in args])
    out = capsys.readouterr()
    return code, out.out + out.err


def join(capsys, hub, name):
    code, out = run(capsys, "join", hub.link, "--name", name)
    assert code == 0, out
    return re.search(r"http://127\.0\.0\.1:\d+/m/[A-Za-z0-9_-]+", out).group(0)


# --- conversation ---------------------------------------------------------------------------------

def test_join_send_wait_roundtrip(hub, capsys):
    k = join(capsys, hub, "codex")
    code, out = run(capsys, "wait", hub.host_key, "--timeout", 2)
    assert "codex (" in out and "joined the session" in out  # the host hears who joined
    code, out = run(capsys, "send", k, "claude", "Hi from Codex 👋 ünïcode")
    assert code == 0 and "Sent message" in out
    code, out = run(capsys, "wait", hub.host_key, "--timeout", 2)
    assert code == 0
    assert "codex -> you:" in out and "Hi from Codex 👋 ünïcode" in out
    assert "send " in out and "wait again" in out  # tells the agent how to answer
    code, out = run(capsys, "send", hub.host_key, "codex", "Hello Codex")
    code, out = run(capsys, "wait", k, "--timeout", 2)
    assert "claude -> you:" in out and "Hello Codex" in out
    code, out = run(capsys, "wait", k, "--timeout", 1)  # nothing is delivered twice
    assert "No new messages" in out and "Hello Codex" not in out


def test_new_member_does_not_hear_its_own_join(hub, capsys):
    k = join(capsys, hub, "codex")
    code, out = run(capsys, "wait", k, "--timeout", 1)
    assert "No new messages" in out


def test_join_prints_commands_and_how_to(hub, capsys):
    code, out = run(capsys, "join", hub.link, "--name", "codex")
    assert 'Joined ccbridge session s-test as "codex"' in out
    assert "In the session: claude (host" in out
    for word in ("send", "wait", "who", "share", "get", "history", "leave"):
        assert f" {word} http://127.0.0.1" in out
    assert "only this chat is connected" in out and "run_in_background" in out


def test_wait_wakes_as_soon_as_a_message_arrives(hub, capsys):
    k = join(capsys, hub, "codex")
    token = k.rsplit("/", 1)[1]
    got = {}

    def waiter():
        t0 = time.monotonic()
        data, _ = ccb.call("GET", f"{hub.base}/api/wait?timeout=30", token, timeout=40)
        got["after"] = time.monotonic() - t0
        got["data"] = data

    th = threading.Thread(target=waiter)
    th.start()
    time.sleep(0.8)
    code, out = run(capsys, "who", hub.host_key)
    assert re.search(r"codex .*listening now", out)
    run(capsys, "send", hub.host_key, "codex", "ping")
    th.join(10)
    assert got["data"]["messages"][0]["text"] == "ping"
    assert got["after"] < 3.0


def test_unacknowledged_messages_come_again(hub, capsys):
    k = join(capsys, hub, "codex")
    token = k.rsplit("/", 1)[1]
    run(capsys, "send", hub.host_key, "codex", "important")
    first, _ = ccb.call("GET", f"{hub.base}/api/wait?timeout=1", token)
    again, _ = ccb.call("GET", f"{hub.base}/api/wait?timeout=1", token)  # e.g. the first wait was cut off
    assert [m["text"] for m in first["messages"]] == [m["text"] for m in again["messages"]] == ["important"]


def test_all_reaches_everyone_but_the_sender(hub, capsys):
    a = join(capsys, hub, "codex")
    b = join(capsys, hub, "cursor")
    run(capsys, "wait", hub.host_key, "--timeout", 1)
    run(capsys, "wait", a, "--timeout", 1)
    run(capsys, "send", b, "all", "hello team")
    assert "hello team" in run(capsys, "wait", hub.host_key, "--timeout", 2)[1]
    assert "hello team" in run(capsys, "wait", a, "--timeout", 2)[1]
    assert "No new messages" in run(capsys, "wait", b, "--timeout", 1)[1]


def test_direct_message_is_private(hub, capsys):
    a = join(capsys, hub, "codex")
    b = join(capsys, hub, "cursor")
    run(capsys, "wait", a, "--timeout", 1)
    run(capsys, "send", hub.host_key, "cursor", "just for cursor")
    assert "just for cursor" in run(capsys, "wait", b, "--timeout", 2)[1]
    assert "just for cursor" not in run(capsys, "wait", a, "--timeout", 1)[1]


def test_send_reports_who_is_listening(hub, capsys):
    join(capsys, hub, "codex")
    code, out = run(capsys, "send", hub.host_key, "codex", "are you there")
    assert "Not listening right now" in out and "codex" in out


def test_unknown_recipient_names_the_members(hub, capsys):
    join(capsys, hub, "codex")
    code, out = run(capsys, "send", hub.host_key, "grok", "hi")
    assert code == 1 and "no member called 'grok'" in out and "codex" in out


def test_same_name_gets_a_suffix(hub, capsys):
    join(capsys, hub, "codex")
    code, out = run(capsys, "join", hub.link, "--name", "codex")
    assert 'as "codex-2"' in out


def test_message_and_history(hub, capsys):
    k = join(capsys, hub, "codex")
    run(capsys, "send", k, "all", "first")
    run(capsys, "send", hub.host_key, "codex", "second")
    code, out = run(capsys, "history", k)
    assert "you -> all:\nfirst" in out and "claude -> you:\nsecond" in out


def test_leave(hub, capsys):
    k = join(capsys, hub, "codex")
    run(capsys, "wait", hub.host_key, "--timeout", 1)
    code, out = run(capsys, "leave", k)
    assert code == 0
    assert "codex left the session" in run(capsys, "wait", hub.host_key, "--timeout", 2)[1]
    code, out = run(capsys, "send", k, "all", "still here?")
    assert code == 2 and "join again" in out


def test_stop_tells_everyone_listening(hub, capsys):
    k = join(capsys, hub, "codex")
    run(capsys, "wait", k, "--timeout", 1)
    token = k.rsplit("/", 1)[1]
    got = {}
    th = threading.Thread(target=lambda: got.update(
        data=ccb.call("GET", f"{hub.base}/api/wait?timeout=30", token, timeout=40)[0]))
    th.start()
    time.sleep(0.5)
    ccb.call("POST", f"{hub.base}/api/stop", payload={}, extra_headers={"X-Admin": "admin-secret"})
    th.join(10)
    assert got["data"]["ended"] is True
    assert any("ended the session" in m["text"] for m in got["data"]["messages"])
    code, out = run(capsys, "wait", k, "--timeout", 1)
    assert code == 2 and "ended" in out


def test_only_the_host_can_stop(hub, capsys):
    with pytest.raises(ccb.Fail):
        ccb.call("POST", f"{hub.base}/api/stop", payload={}, extra_headers={"X-Admin": "guess"})
    assert not hub.hub.ended


# --- links, keys -------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "https://abc-def.trycloudflare.com/j/ABCD-EFGH-JKLM",
    "https\\://abc-def.trycloudflare.com/j/ABCD-EFGH-JKLM",           # Codex's chat box escapes ':'
    "https/://abc-def.trycloudflare.com/j/ABCD-EFGH-JKLM",            # Git Bash rewrites that
    "<https://abc-def.trycloudflare.com/j/ABCD-EFGH-JKLM>.",
    "[join](https://abc-def.trycloudflare.com/j/abcd-efgh-jklm)",
    "`https://abc-def.trycloudflare.com/j/ABCDEFGHJKLM`",
    "Join my ccbridge session: python ccb.py join https://abc-def.trycloudflare.com/j/ABCD-EFGH-JKLM",
    "​https://abc-def.trycloudflare.com/j/ABCD-EFGH-JKLM ",
])
def test_links_as_people_paste_them(text):
    assert ccb.parse_link(text) == ("https://abc-def.trycloudflare.com", "ABCD-EFGH-JKLM")


@pytest.mark.parametrize("text", ["https://example.com", "ABCD-EFGH-JKLM", "https://x.com/j/ABC"])
def test_not_a_link(text):
    with pytest.raises(ccb.Fail, match="not a ccbridge join link"):
        ccb.parse_link(text)


def test_wrong_code_is_refused_then_throttled(hub, capsys):
    bad = f"{hub.base}/j/ZZZZ-ZZZZ-ZZZZ"
    code, out = run(capsys, "join", bad)
    assert code == 1 and "not valid" in out
    for _ in range(30):
        with pytest.raises(ccb.Fail):
            ccb.call("POST", f"{hub.base}/api/join", payload={"code": "ZZZZ-ZZZZ-ZZZZ"})
    code, out = run(capsys, "join", hub.link)
    assert code == 1 and "too many tries" in out


def test_bad_key(hub, capsys):
    code, out = run(capsys, "who", f"{hub.base}/m/{'x' * 24}")
    assert code == 2 and "join again" in out
    code, out = run(capsys, "who", "not-a-key")
    assert code == 1 and "not a ccbridge key" in out


def test_this_computers_hub_is_used_directly(hub, capsys, tmp_path):
    public = "https://made-up-name.trycloudflare.com"
    ccb.write_private(ccb.hub_file(), json.dumps({"public": public, "local": hub.base}))
    code, out = run(capsys, "join", f"{public}/j/{CODE}", "--name", "codex")
    assert code == 0 and f"{hub.base}/m/" in out
    key = re.search(r"\S+/m/[A-Za-z0-9_-]+", out).group(0)
    assert ccb.parse_key(key.replace(hub.base, public))[0] == hub.base


def test_a_tunnel_that_is_still_starting_is_retried_then_explained(monkeypatch):
    import urllib.error
    calls = []

    class Opener:
        def open(self, req, timeout):
            calls.append(1)
            raise urllib.error.HTTPError(req.full_url, 530, "origin unreachable", {}, __import__("io").BytesIO(b"<html>"))

    monkeypatch.setattr(ccb.urllib.request, "build_opener", lambda *h: Opener())
    monkeypatch.setattr(ccb.time, "sleep", lambda s: None)
    with pytest.raises(ccb.Fail, match="not reachable right now"):
        ccb.call("GET", "https://x-y.trycloudflare.com/healthz", retry_for=0.05)
    assert len(calls) >= 2


def test_join_page_and_script_download(hub):
    page, _ = ccb.call("GET", hub.link)
    assert b"python ccb.py join" in page and f"/j/{CODE}".encode() in page and b"/ccb.py" in page
    script, _ = ccb.call("GET", f"{hub.base}/ccb.py")
    assert script == Path(ccb.__file__).read_bytes()
    with pytest.raises(ccb.Fail):
        ccb.call("GET", f"{hub.base}/j/ZZZZ-ZZZZ-ZZZZ")


# --- files -------------------------------------------------------------------------------------------

def test_file_arrives_byte_identical(hub, capsys, tmp_path, monkeypatch):
    k = join(capsys, hub, "codex")
    src = tmp_path / "data.bin"
    src.write_bytes(os.urandom(700_000))
    code, out = run(capsys, "share", k, src, "--to", "claude", "--note", "the dataset")
    assert code == 0 and "as file f1" in out
    code, out = run(capsys, "wait", hub.host_key, "--timeout", 2)
    assert "shared a file: data.bin" in out and "the dataset" in out and " get " in out
    dest = tmp_path / "dl"
    dest.mkdir()
    monkeypatch.chdir(dest)
    code, out = run(capsys, "get", hub.host_key, "f1")
    assert code == 0
    assert hashlib.sha256((dest / "data.bin").read_bytes()).digest() == hashlib.sha256(src.read_bytes()).digest()
    code, out = run(capsys, "get", hub.host_key, "1")
    assert code == 1 and "already exists" in out


def test_a_file_and_the_message_after_it_arrive_together(hub, capsys, tmp_path):
    k = join(capsys, hub, "codex")
    f = tmp_path / "spec.md"
    f.write_text("# spec\n")
    token = hub.host_token
    got = {}
    th = threading.Thread(target=lambda: got.update(
        data=ccb.call("GET", f"{hub.base}/api/wait?timeout=30", token, timeout=40)[0]))
    run(capsys, "wait", hub.host_key, "--timeout", 1)
    th.start()
    time.sleep(0.3)
    run(capsys, "share", k, f, "--to", "claude")
    time.sleep(0.8)
    run(capsys, "send", k, "claude", "please review spec.md")
    th.join(10)
    assert [m["kind"] for m in got["data"]["messages"]] == ["file", "text"]


def test_file_for_one_member_is_not_downloadable_by_others(hub, capsys, tmp_path):
    a = join(capsys, hub, "codex")
    b = join(capsys, hub, "cursor")
    f = tmp_path / "x.txt"
    f.write_text("for claude only")
    run(capsys, "share", a, f, "--to", "claude")
    code, out = run(capsys, "get", b, "f1", "--out", tmp_path / "y.txt")
    assert code == 1 and "no such file" in out


def test_folder_is_zipped_without_junk_or_credentials(hub, capsys, tmp_path, monkeypatch):
    k = join(capsys, hub, "codex")
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "app.py").write_text("print('hi')\n")
    (proj / ".git").mkdir()
    (proj / ".git" / "HEAD").write_text("ref")
    (proj / ".env").write_text("X=1")
    (proj / ".env.example").write_text("X=")
    code, out = run(capsys, "share", k, proj)
    assert code == 0 and "proj.zip" in out and ".env" in out  # says what it left out
    out_dir = tmp_path / "unzipped"
    code, out = run(capsys, "get", hub.host_key, "f1", "--unzip", "--out", out_dir)
    assert code == 0
    got = sorted(p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*") if p.is_file())
    assert got == ["proj/.env.example", "proj/src/app.py"]


def test_unzip_refuses_paths_outside(tmp_path):
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.txt", "x")
    with pytest.raises(ccb.Fail, match="outside"):
        ccb.safe_extract(zipfile.ZipFile(buf), tmp_path / "d", False)


def test_credentials_are_not_sent_by_accident(hub, capsys, tmp_path):
    k = join(capsys, hub, "codex")
    token = "ghp_" + "a" * 36
    code, out = run(capsys, "send", k, "all", f"use {token}")
    assert code == 1 and "credential" in out
    code, out = run(capsys, "send", k, "all", f"use {token}", "--force")
    assert code == 0
    env = tmp_path / ".env"
    env.write_text("SECRET=1")
    code, out = run(capsys, "share", k, env)
    assert code == 1 and "credentials file" in out
    leaky = tmp_path / "config.py"
    leaky.write_text("-----BEGIN RSA PRIVATE KEY-----\nabc\n")
    code, out = run(capsys, "share", k, leaky)
    assert code == 1 and "credential" in out


def test_send_reads_a_file_or_stdin(hub, capsys, tmp_path, monkeypatch):
    k = join(capsys, hub, "codex")
    msg = tmp_path / "m.txt"
    msg.write_text('line one\nline "two"\n', encoding="utf-8")
    run(capsys, "send", k, "claude", "--file", msg)
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("from stdin"))
    run(capsys, "send", k, "claude", "-")
    out = run(capsys, "wait", hub.host_key, "--timeout", 2)[1]
    assert 'line one\nline "two"' in out and "from stdin" in out


# --- the real thing: host starts a background hub, a separate process joins ---------------------------

def test_host_join_stop_as_separate_processes(tmp_path):
    env = {**os.environ, "CCB_HOME": str(tmp_path / "home"), "PYTHONIOENCODING": "utf-8"}
    script = str(Path(ccb.__file__).resolve())

    def cli(*args, timeout=60):
        r = subprocess.run([sys.executable, script, *args], env=env, capture_output=True, text=True,
                           encoding="utf-8", timeout=timeout, cwd=str(tmp_path))
        return r.returncode, r.stdout + r.stderr

    code, out = cli("host", "--local", "--port", "0", "--name", "claude")
    try:
        assert code == 0, out
        link = re.search(r"Join my ccbridge session: .* join (http://127\.0\.0\.1:\d+/j/[A-Z0-9-]+)", out).group(1)
        host_key = re.search(r"Your key is (\S+)", out).group(1)
        code, again = cli("host", "--local", "--port", "0")  # a second host call shows the same session
        assert "already running" in again and link in again
        code, joined = cli("join", link, "--name", "codex")
        assert code == 0, joined
        key = re.search(r"Your key is (\S+)", joined).group(1)
        cli("send", key, "claude", "hello host")
        code, heard = cli("wait", host_key, "--timeout", "5")
        assert "hello host" in heard
    finally:
        code, out = cli("stop")
    assert "ended" in out
    assert not (tmp_path / "home" / "hub.json").exists()
    code, out = cli("wait", key, "--timeout", "2")
    assert code != 0
