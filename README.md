# ccbridge

[![tests](https://github.com/sourabhnirvani/ccbridge/actions/workflows/tests.yml/badge.svg)](https://github.com/sourabhnirvani/ccbridge/actions/workflows/tests.yml)

**Put AI coding agents from different IDEs - on your laptop or on other people's - into one live session.**

One agent hosts and gets a link. You paste one line into another IDE's agent chat (Codex, Claude Code,
Cursor, Antigravity, ...). That chat joins, and from then on the agents talk to each other, hand over
files and work on the project together, like a team.

- **Only the chat you paste into is connected.** Nothing is written into your project or your tools'
  settings, and no other chat or window joins.
- **One file, no installs.** `ccb.py` needs Python 3.8+ and nothing else. The host's link even serves it,
  so another computer can download it.
- **Live.** A waiting agent gets a message within about a second.

## Use it

**1. Host.** Run this in the agent chat that starts the session, or in a terminal:

```bash
python path/to/ccb.py host
```

The hub starts in the background and keeps running after the command returns. `host` prints the agent's own
commands and a line to paste, for example:

```
PASTE THIS into the agent chat of another IDE on THIS computer:

Join my ccbridge session: python C:/Projects/ccbridge/ccb.py join http://127.0.0.1:8787/j/Y94K-MPHA-R5T5

For an IDE on ANOTHER computer, paste this instead (it needs Python 3.8+, nothing else):

Join my ccbridge session: download https://<words>.trycloudflare.com/ccb.py and run: python ccb.py join https://<words>.trycloudflare.com/j/Y94K-MPHA-R5T5
```

**2. Join.** Paste that line into the other IDE's agent chat. The agent runs `join`, says hello and starts
listening. Each agent gets a private key and these commands:

| Command | What it does |
|---|---|
| `send <key> <name\|all> "text"` | message one member or everyone (`--file msg.txt`, or `-` to read stdin, for long text) |
| `wait <key>` | listen: blocks until messages arrive, then prints them (by default it gives up after 300 s) |
| `who <key>` | who is in the session, and who is listening right now |
| `share <key> <file or folder> [--to name] [--note "..."]` | hand over a file, or a folder as a zip |
| `get <key> <file-id> [--out path] [--unzip]` | download a shared file |
| `history <key>` | earlier messages |
| `leave <key>` | leave the session |

**3. Stop.** Run `python ccb.py stop` on the host to end the session for everyone. A session nobody uses
for 12 hours ends by itself.

## How agents stay in the conversation

An agent hears messages only while it runs `wait`. So `join` tells it to loop: wait, act, `send` the answer,
wait again. It keeps going until its user stops it or the session ends.

- **Claude Code** runs `wait` in the background and is woken the moment a message arrives. You can keep
  talking to it in the meantime.
- **Codex** and other agents keep `wait` running in the foreground; Codex uses `--timeout 60` per round.
  While an agent is in that loop, it answers on its own. If you interrupt it, tell it to keep listening.
- An agent that stopped listening gets its messages the next time it runs `wait`. `send` tells the sender
  which members are listening now.

## Sandboxes and networks

**Same computer.** A join link that points at this computer's hub, even the public one, goes straight to
`127.0.0.1`. Codex's read-only sandbox allows that, so nothing needs approving.

**Another computer.** The public link is a [Cloudflare quick tunnel](https://github.com/cloudflare/cloudflared)
(`winget install --id Cloudflare.cloudflared`). Without cloudflared, or with `host --local`, only this
computer can join.
- Codex's sandbox blocks the internet, so the joiner approves the command once. Allowing every command that
  starts with `python .../ccb.py` saves asking again.
- A new tunnel address takes a short while to work; `host` waits for it.

**Downloads.** `get` writes into the project, so a read-only sandbox asks first.

## Safety

- **The link is the key.** Anyone with it can join the session while it runs, so send it privately. Each
  member gets its own random key, and wrong links are throttled.
- **Peers are other agents.**
  - Every batch of messages is labelled as written by other AI agents.
  - A peer can ask, but the receiving agent's own user and permissions come first. Nobody in the session
    can approve anything for another person's agent.
  - ccbridge never runs anything by itself. Agents act with their own tool's normal permissions.
- **No secrets by accident.**
  - `send` and `share` refuse text that looks like a private key or an API token.
  - They also refuse credential files (`.env`, `*.pem`, `id_rsa`, ...).
  - A folder share leaves those files out, along with `.git`, `node_modules` and virtualenvs.
  - `--force` overrides the guard when sharing is really safe.
- **In memory, on the host.** Messages and files live in the hub's memory on the host's computer and are
  gone when the session ends. There is no end-to-end encryption: the Cloudflare tunnel ends TLS at
  Cloudflare.

## Tests

```bash
python -m pip install pytest
python -m pytest
```

37 tests run a real hub on a free port and drive it with the same commands agents use. They cover:
- conversation, delivery and redelivery
- private and broadcast messages
- files and folders arriving byte-identical
- links as chat boxes mangle them
- throttling, the credential guards, and stop/leave
- `host` → `join` → `stop` as separate processes

Tested live on Windows in November 2026:
- **Claude Code host, real Codex joiner.** This repo's host ran in Claude Code. A real Codex agent
  (`codex exec`, workspace-write sandbox) joined from the pasted line. It said hello and took a task. It
  edited a file and shared it back, downloaded a file and returned the matching SHA-256, then left.
- **Public tunnel.** A member joined through the public link. Delivery was live, and a 1.9 MB upload
  went through.

## License

MIT
