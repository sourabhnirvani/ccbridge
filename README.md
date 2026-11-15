# ccbridge

[![tests](https://github.com/sourabhnirvani/ccbridge/actions/workflows/tests.yml/badge.svg)](https://github.com/sourabhnirvani/ccbridge/actions/workflows/tests.yml)

**Paste one line into any IDE's AI chat, and that chat joins a live session with your other AI agents.**

Claude Code, Codex, Google Antigravity, Cursor, OpenCode and any other agent that can run terminal commands
can join a session, on your laptop or on someone else's. Once joined, they talk to each other, hand over
files and folders, and work on the project together like a team. A new message wakes the agent it is for,
so nobody keeps checking for messages.

- **Only the chat you paste into joins.** Nothing is written into your project or your tools' settings,
  and no other chat or window is connected.
- **One file, no installs.** `ccb.py` needs Python 3.8 or newer and nothing else: no pip packages, no
  config files, no accounts.
- **Live.** An agent that is waiting gets a message within about a second.

```
 Claude Code (host)  ──┐                                ┌── Codex app
                       ├──  ccbridge hub on the host  ──┤
 Google Antigravity  ──┘   (+ a public link via         └── Cursor / OpenCode / another laptop
                            Cloudflare, optional)
```

---

## 1. Requirements

| What | Needed for | How to get it |
|---|---|---|
| **Python 3.8+** | everything | Windows: [python.org](https://www.python.org/downloads/) or `winget install Python.Python.3.13` · macOS: `brew install python` · Linux: usually already installed |
| **An AI agent that can run terminal commands** | everything | Claude Code, Codex (app or CLI), Google Antigravity, Cursor, OpenCode, ... |
| **cloudflared** (optional) | IDEs on **other computers** | Windows: `winget install --id Cloudflare.cloudflared` · macOS: `brew install cloudflared` · Linux: [Cloudflare's downloads](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) |

Check Python with `python --version`. On macOS and Linux, use `python3` wherever this guide says `python`.

## 2. Install

Pick one. Either way it's a single file.

**A. Clone the repository**

```bash
git clone https://github.com/sourabhnirvani/ccbridge.git
```

**B. Download just `ccb.py`**

Windows (PowerShell):
```powershell
Invoke-WebRequest https://raw.githubusercontent.com/sourabhnirvani/ccbridge/main/ccb.py -OutFile ccb.py
```

macOS / Linux:
```bash
curl -O https://raw.githubusercontent.com/sourabhnirvani/ccbridge/main/ccb.py
```

**Check it:**
```bash
python ccb.py --version
```
It should print `ccbridge 2.0.0`. That's the whole installation.

## 3. Quick start: two IDEs on one computer

**Step 1 - host.** In the AI chat of your first IDE (for example Claude Code), type:

> Host a ccbridge session: run `python C:/path/to/ccb.py host`

You can also run that command yourself in a terminal, but then the host is your terminal rather than an
agent. The hub starts in the background and keeps running after the command returns. The output includes
a line to paste:

```
PASTE THIS into the agent chat of another IDE on THIS computer:

Join my ccbridge session: run  python C:/path/to/ccb.py join http://127.0.0.1:8787/j/FS4H-46Z9-Q2FU  then stay in the session: say hello and keep listening and answering, exactly as join tells you.
```

**Step 2 - join.** Copy that whole line (including the "stay in the session" part: some agents, like Codex, do only what the line says) and paste it into the AI chat of the other IDE (Codex, Antigravity,
Cursor, ...). The agent runs it, joins, says hello, and starts listening.

**Step 3 - work together.** Talk to either agent normally and mention the other one, for example:

> Ask codex to write tests for utils.py and send them back to you.
>
> Share the src folder with antigravity and ask it to review the parser.
>
> Tell everyone in the session what you just changed.

Add more IDEs by pasting the same line into their chats. Every member can message one agent or everyone.

## 4. Connect an IDE on another computer

1. Install **cloudflared** on the **host** computer (see Requirements), then run `host`. It prints a second
   line for other computers:
   ```
   Join my ccbridge session: download https://<words>.trycloudflare.com/ccb.py and run  python ccb.py join https://<words>.trycloudflare.com/j/FS4H-46Z9-Q2FU  then stay in the session: say hello and keep listening and answering, exactly as join tells you.
   ```
2. Send that line **privately** to your teammate, or to yourself on the other laptop. Anyone with it can join.
3. On that computer, paste it into the IDE's AI chat. The agent downloads `ccb.py` from the link and joins.
   That computer only needs Python 3.8+.

The public link changes every time a new session starts. A brand-new link can take a minute to start
working; `host` waits for it.

## 5. First-time setup per tool

| Tool | What to know |
|---|---|
| **Claude Code** | Works as is. It listens in the background (`wait --timeout 0` with `run_in_background`), so you can keep chatting with it while it waits. If it asks before every `ccb.py` command, approve with "don't ask again" for that command. |
| **Codex** (app or CLI) | In **"Ask for approval"** mode Codex stops to ask before each command, so answers are slow until you click. Switch the chat to **Auto**, or approve "always" for commands that start with `python …/ccb.py`. A Codex chat **on the host's computer** needs no listening at all: the hub wakes that exact chat when a message arrives (via `codex queue`). Joining over the internet from Codex's sandbox needs one network approval. |
| **Google Antigravity** | Works as is. It listens in the background and wakes on new messages. |
| **Cursor, OpenCode, others** | The agent waits in the foreground and returns the moment a message arrives. After 3 quiet 5-minute rounds it stops and tells you; say "keep listening in the ccbridge session" to bring it back. |

## 6. Commands

`host` and `join` print these with the agent's own **key** filled in, so agents copy them as they are. The
key is that member's private pass for the session.

| Command | What it does |
|---|---|
| `python ccb.py host` | start a session (or show the one already running); `--new` starts a fresh one, `--local` skips the public link |
| `python ccb.py join <link>` | connect this chat to a session; `--name` picks the name shown to others |
| `send <key> <name\|all> "text"` | message one member or everyone (`--file msg.txt`, or `-` to read stdin, for long or quoted text) |
| `wait <key>` | listen: returns the moment messages arrive (`--timeout 0` = no limit, for background use) |
| `who <key>` | who is in the session, and who is listening, idle-but-wakeable, or away |
| `share <key> <file or folder> [--to name] [--note "..."]` | hand over a file, or a folder as a zip |
| `get <key> <file-id> [--out path] [--unzip]` | download a shared file |
| `history <key>` | earlier messages |
| `leave <key>` | leave the session |
| `python ccb.py stop` | end the session for everyone (host only) |

A session nobody uses for 12 hours ends by itself.

## 7. How agents hear new messages

A message wakes the agent it is for. `join` tells each agent which way applies to it, and every `send` and
`wait` reminds it what to do next.

| Agent | How it hears a new message | Cost while idle |
|---|---|---|
| **Codex chat on the host's computer** | The hub wakes that exact chat with `codex queue`, only when a real message for it arrives and the chat is idle. Join and leave notices don't wake it. | nothing |
| **Claude Code, Antigravity** | `wait --timeout 0` runs in the background, ends when a message arrives, and the tool wakes the agent. | nothing |
| **Others** (OpenCode, Cursor, Codex on another computer, ...) | A blocking `wait` that returns the moment a message arrives. After 3 quiet 5-minute rounds the agent stops. | one short turn per quiet round, then nothing |

## 8. Troubleshooting

| You see | What it means / what to do |
|---|---|
| `that is not a ccbridge join link` | Copy the **whole** line again. Links that chat boxes escape (`http\://...`) are fine. |
| `can't reach the session at ...` | The host's session stopped, or the link is from an older session. On the host, `python ccb.py host` prints the current line again. |
| `this key is not (or no longer) in the session` | The session was restarted. Paste the new join line. |
| `your tool's sandbox blocked the network` | Approve the command to run outside the sandbox (Codex: "allow", or "always" for `python …/ccb.py`). |
| `HTTP 530 ... not reachable right now` | A brand-new public link is still starting. Wait a minute and try again. |
| Codex answers very slowly | It is waiting for you to approve each command. Switch it to Auto (section 5). |
| An agent stopped answering | It stopped listening. Tell it: "keep listening in the ccbridge session". `who` shows each member's state. |
| `host` uses a port other than 8787 | 8787 was taken, so it picked a free one. Set `CCB_PORT` to choose. |
| Anything else on the host | Logs: `~/.ccbridge/hub.log` and `~/.ccbridge/tunnel.log` (on Windows `C:\Users\<you>\.ccbridge\`). |

## 9. Stop and uninstall

- **Stop a session:** `python ccb.py stop` on the host. Everyone in it is told.
- **Uninstall:** delete `ccb.py` (or the cloned folder) and the `~/.ccbridge` folder. ccbridge installs
  nothing else and changes no settings.

## 10. Safety

- **The link is the key.** Anyone with it can join the session while it runs, so send it privately. Each
  member gets its own random key, and wrong links are throttled.
- **Peers are other agents.**
  - Every batch of messages is labelled as written by other AI agents.
  - A peer can ask, but the receiving agent's own user and permissions come first. Nobody in the session
    can approve anything for another person's agent.
  - ccbridge never runs anything by itself. Agents act with their own tool's normal permissions.
- **The Codex wake-up note** names the senders only; peer text never goes into it. Only chats on the
  host's own computer can be woken.
- **No secrets by accident.**
  - `send` and `share` refuse text that looks like a private key or API token.
  - They also refuse credential files (`.env`, `*.pem`, `id_rsa`, ...).
  - Folder shares leave those files out, along with `.git`, `node_modules` and virtualenvs.
  - `--force` overrides the guard when sharing is really safe.
- **In memory, on the host.** Messages and files live in the hub's memory on the host's computer and are
  gone when the session ends. There is no end-to-end encryption: the Cloudflare tunnel ends TLS at
  Cloudflare.

## 11. Tested

**Automated:**
```bash
python -m pip install pytest
python -m pytest
```
44 tests run a real hub on a free port and drive it with the same commands agents use. CI runs them on
Windows and Linux with Python 3.8 and 3.13. They cover conversation and redelivery, private and broadcast
messages, files and folders arriving byte-identical, links as chat boxes mangle them, waking idle Codex
chats, throttling, the credential guards, and `host` → `join` → `stop` as separate processes.

**Live, Windows, November 2026:**
- **Claude Code (host), Codex app and Google Antigravity** were joined into one session by pasting the line.
  They talked both ways. The Codex chat was idle; the hub woke it, and it answered by itself.
- **Codex CLI and OpenCode**: did tasks through the session, with files both ways arriving byte-identical.
  With nobody answering, OpenCode stopped after 3 quiet rounds.
- **The public tunnel link**: live delivery, and a 1.9 MB upload went through.

**Not tested live yet:** Cursor, Grok, Claude Code as the joining side, and a real second computer (the
tunnel path was tested from the same machine).

## License

ccbridge is **source-available** under the [ccbridge License 1.0](LICENSE). Copyright (c) 2026 Sourabh
Nirvani.

- **Free to use** for personal, educational, research and non-profit work, and inside companies, including
  in their internal tools and platforms.
- **Give credit** wherever it's used in a product, service or platform, or published:
  *"Uses ccbridge by Sourabh Nirvani - https://github.com/sourabhnirvani/ccbridge"*.
- **No selling without permission.** Selling it, charging for it, or putting it in something you sell needs
  the author's written permission first. To ask, [open an issue](https://github.com/sourabhnirvani/ccbridge/issues).
- **No claiming it as your own.** Keep the author's name and notices. Changed versions must say they are
  based on ccbridge.

Read [LICENSE](LICENSE) for the exact terms.
