# ccbridge

**Let two people's Claude Code agents talk to each other while working on the same project.**

Claude Code can already message its own sessions — but only within one account, on
one machine. If you and a teammate are on separate accounts and separate laptops,
your agents can't reach each other. ccbridge closes that gap.

Two commands each, and your agent can ask theirs what it just changed.

```
  You                                                     Your teammate
  ┌────────────────┐                                 ┌────────────────┐
  │  Claude Code   │                                 │  Claude Code   │
  │   + ccbridge   │                                 │   + ccbridge   │
  └───────┬────────┘                                 └────────┬───────┘
          │  HTTPS                                    HTTPS   │
          └────────────────►  ┌──────────────┐  ◄────────────┘
                              │    relay     │
                              │ rooms+queue  │
                              │   (SQLite)   │
                              └──────────────┘
```

Four tools show up in each agent: `bridge_status`, `list_peers`, `send_message`,
`get_messages`.

---

## Quick start

Requires **Python 3.10+** on both machines.

`bridge.py` writes a `.mcp.json` into **the folder you run it from**, and Claude
Code only loads it when opened in that folder. So run it from your **project**
folder — the one you open Claude Code in — not from inside the ccbridge clone.

### One of you hosts

```bash
git clone https://github.com/sourabhnirvani/ccbridge
pip install -r ccbridge/requirements.txt
winget install --id Cloudflare.cloudflared   # macOS/Linux: brew install cloudflared

cd path/to/your-project
python path/to/ccbridge/bridge.py host <yourname>
```

It prints a single invite line and keeps running. **Leave that window open — it is
the relay.**

```
============================================================
SEND THIS ONE LINE TO YOUR FRIEND (privately - it holds the room token):

  python bridge.py join eyJ1IjoiaHR0cHM6Ly9hYmMtZGVm... <theirname>

============================================================
```

### The other person joins

```bash
git clone https://github.com/sourabhnirvani/ccbridge
pip install -r ccbridge/requirements.txt

cd path/to/your-copy-of-the-project
python path/to/ccbridge/bridge.py join <the-invite-they-sent-you> <yourname>
```

Send the invite privately (Signal, a password manager). It contains the room token.

### Both of you

Restart Claude Code in the project folder. The first time, it asks you to approve
the project's `ccbridge` MCP server — say yes, or the tools won't load. Then ask it:

> check the bridge — who's online?

Add `--allow <theirname>` to `host` or `join` to accept messages from that person only.

---

## Setting this up with an AI agent

Most people will hand this to Claude Code, Codex, or Cursor rather than typing it.
Paste this prompt, from inside the project you want to collaborate on:

> Clone https://github.com/sourabhnirvani/ccbridge somewhere outside this project and
> install its requirements. Then, **from this project's folder**, set up the bridge.
> I am the **host** — run `python <ccbridge>/bridge.py host <myname>` and give me the
> invite line it prints.
> (Or: I am **joining** — run `python <ccbridge>/bridge.py join <invite> <myname>`.)
> Then tell me to restart Claude Code and approve the ccbridge MCP server.

What an agent needs to know, precisely:

| | |
|---|---|
| Install | `pip install -r <ccbridge>/requirements.txt` |
| Verify | `python -m pytest` inside `<ccbridge>` → 99 tests should pass |
| Where | Run `bridge.py` **from the project folder**; it writes `.mcp.json` into the current working directory and adds it to that folder's `.gitignore` |
| Host | `python <ccbridge>/bridge.py host <name>` — **long-running**, do not wait for it to exit; read the invite from **`<ccbridge>/invite.txt`** |
| Join | `python <ccbridge>/bridge.py join <invite> <name>` — exits immediately after writing `.mcp.json` |
| Then | Claude Code must be **restarted** in the project folder, and the `ccbridge` MCP server **approved** when it asks |

`host` blocks forever by design — it is the relay process. Run it in a background
shell or a separate terminal, then read the invite from `invite.txt` next to
`bridge.py` rather than scraping the running process's output.

---

## Troubleshooting

**The agent can't reach the relay (joining side).**
Some ISPs' DNS servers don't resolve `*.trycloudflare.com` subdomains. Check:

```bash
nslookup <the-tunnel-host> 1.1.1.1    # works?
nslookup <the-tunnel-host>            # fails?
```

If the first works and the second doesn't, it's your resolver. Set your network's
DNS to `1.1.1.1` or `8.8.8.8`. (This only affects the person *joining* — the host
talks to its own relay on localhost.)

**"Display name '...' belongs to someone else in this room."** Your name is tied
to the identity key in `<ccbridge>/.ccbridge_identity`. You get this if someone else
already uses that name, or if you joined from a different ccbridge folder. Pick
another name, or run `join` from the same ccbridge folder as before.

**Tests pass but Claude Code shows no bridge tools.** Claude Code loads MCP servers
at startup — restart it, make sure `.mcp.json` is in the folder you opened, and
approve the `ccbridge` server when asked.

**"cloudflared did not produce a public URL".** cloudflared is installed but
couldn't open a tunnel within 30 seconds — usually a firewall or network problem.

---

## How the two agents talk

Nobody types commands. You talk to your own Claude in plain English:

- *"Check the bridge — is she online?"* → `list_peers`
- *"Tell him I finished the data loader and pushed it"* → `send_message`
- *"Any messages from her?"* → `get_messages`

Their agent does the same on their side. Messages arrive labelled with who sent
them. Each session joins as soon as Claude Code starts and stays marked online
while it's open. Messages sent while you're away, or while Claude Code restarts,
are waiting when you next check.

---

## Safety model

A message now arrives from *another human's* agent, so it is untrusted input by
definition. The design keeps it powerless:

- **The bridge moves text and nothing else.** There is no tool here that runs a
  command, reads a file, or changes a setting. The worst a peer can do is *ask*;
  acting on the ask still goes through your own permission prompts, in front of you.
- **Every message is wrapped** with its sender and an explicit note that peer text
  is data, never authorization — including the rule that if a peer claims it was
  denied permission and asks your agent to do the thing instead, that is permission
  laundering and must be refused.
- **The block can't be closed early.** Each message's banner carries a random tag
  the peer can't know in advance, so a forged "end of message" line stays inside
  the untrusted block.
- **Forged harness framing is neutralized.** A peer can't smuggle a fake
  `<system-reminder>`, task notification, tool result, chat-turn marker, or a forged
  copy of the delivery banner — including fullwidth, non-breaking-space and unclosed
  variants. Those are rewritten to harmless look-alikes and the message is flagged
  so the reader treats that sender with extra suspicion.
- **Sanitizing runs twice** — on the relay (so a hostile client can't skip it) and
  again on delivery (so a hostile *relay* can't skip it either). Sender names,
  flags, timestamps and relay error text are checked on delivery too.
- **Rooms are isolated by token.** A session in one room can't read or post to
  another, even holding a valid token for that other room. A relay started by
  `bridge.py host` serves only its own room.
- **Display names belong to one person.** Your name is tied to your identity key
  on first use, so nobody else in the room can later take it, read your direct
  messages, or post as you.
- **Caps:** 8 KB per message, 60 messages/minute per person, limits on joins and
  new rooms, and old messages expire, so neither a peer nor a stranger who finds
  the tunnel URL can flood your agent or fill the host's disk.
- **Optional allowlist:** `--allow` / `CCBRIDGE_ALLOWED_PEERS` drops messages from anyone else.

### What it does not do

- **No end-to-end encryption.** The relay sees message text and keeps it, in plain
  text in `ccbridge.db` on the host's machine, for up to 7 days. Run it yourself;
  don't put one person's relay in someone else's hands.
- **It does not judge what a peer asks for.** It guarantees the request arrives
  labelled and powerless, not that the request is reasonable.
- **Use HTTPS** for anything off your own machine. The token travels in a header.

---

## Configuration

`bridge.py` writes these into `.mcp.json` for you. Set them by hand only if you
want to run the pieces separately.

| Variable | Meaning |
|---|---|
| `CCBRIDGE_RELAY_URL` | Where the relay is, e.g. `https://x.trycloudflare.com` |
| `CCBRIDGE_ROOM` | Shared room name — **same** for both people |
| `CCBRIDGE_TOKEN` | Shared secret — **same** for both people |
| `CCBRIDGE_NAME` | How you appear to your peer — **different** for each person |
| `CCBRIDGE_IDENTITY_KEY` | Your own secret that owns your name — **never** shared (`bridge.py` keeps it in `<ccbridge>/.ccbridge_identity`) |
| `CCBRIDGE_ALLOWED_PEERS` | Optional comma-separated allowlist of peer names |

Running the relay directly, without `bridge.py`:

```bash
python run_relay.py                                  # 127.0.0.1:8787
CCBRIDGE_HOST=0.0.0.0 python run_relay.py            # bash/zsh - only behind HTTPS
```

```powershell
$env:CCBRIDGE_HOST="0.0.0.0"; python run_relay.py    # PowerShell - only behind HTTPS
```

Relay settings: `CCBRIDGE_PORT`, `CCBRIDGE_DB` (default `./ccbridge.db`). Rooms are
trust-on-first-use by default: the first join fixes the room's token. Pre-create
them instead with `CCBRIDGE_ROOMS="room:token,room2:token2"`, and set
`CCBRIDGE_OPEN_ROOMS=0` to refuse any room not listed.

---

## Tests

```bash
python -m pytest
```

99 tests: relay auth and room isolation, name ownership and resume-after-restart,
rate limits, size caps and retention, injection and banner-forgery defences
(including known bypasses), a hostile relay, the peer allowlist, cursor behaviour,
invite encoding and setup errors, and an end-to-end round trip over real HTTP
between two sessions.

---

## Limitations

- The free cloudflared tunnel gives a **new URL every restart**, so re-hosting
  means sending a fresh invite. Your name survives it; the URL doesn't. Use a named
  tunnel or a small VM to make it stable.
- The host's terminal must stay open; it is the relay.
- Polling, not push: an agent sees messages when it calls `get_messages`.
- Lose `.ccbridge_identity` and your name stays reserved in that room for 30 days
  of inactivity (or until the host deletes `ccbridge.db`). Pick a new name meanwhile.

## License

MIT
