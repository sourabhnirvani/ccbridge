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

It claims your name, prints a single invite line and keeps running. **Leave that
window open — it is the relay.** Each project gets a room of its own, and keeps it
every time you host from there (even if you move or rename the folder; a copy of
the folder gets its own); `--new-room` starts over with a fresh room and
token (say, if an invite leaked).

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

`join` contacts the bridge and claims your name right away, so a taken name or an
out-of-date invite shows up here rather than later inside Claude Code.

### Both of you

Restart Claude Code in the project folder. The first time, it asks you to approve
the project's `ccbridge` MCP server — say yes, or the tools won't load. Then ask it:

> check the bridge — who's online?

Add `--allow <theirname>` to `host` to lock the room to the two of you: nobody
else can join, even with the invite. On `join`, it accepts messages from that
person only.

Neither command puts a secret in `.mcp.json`: the room token and your identity
key go into a settings file inside the ccbridge folder (readable only by you, on
macOS and Linux), and `.mcp.json` just names it. So it is safe in a project that commits its
`.mcp.json` — though its `ccbridge` entry only works on your machine, so leave
it out of commits (`bridge.py` reminds you if git tracks the file).

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
| Verify | `python -m pytest` inside `<ccbridge>` → 261 tests should pass (on Windows, 260 and 1 skipped) |
| Where | Run `bridge.py` **from the project folder**; it writes `.mcp.json` into the current working directory (no secrets in it) and adds it to that folder's `.gitignore` |
| Host | **First delete `<ccbridge>/invite.txt`** if it exists, then run `python <ccbridge>/bridge.py host <name>` — **long-running**, do not wait for it to exit. Wait for `<ccbridge>/invite.txt` to appear (it does once the bridge is ready) and read the invite from there |
| Join | `python <ccbridge>/bridge.py join <invite> <name>` — contacts the bridge, claims the name, writes `.mcp.json`, exits. Exit code 0: joined. 1: refused (name taken, invite out of date, room locked), nothing written. 2: no bridge answered — settings saved anyway, but ask the host for a current invite |
| Then | Claude Code must be **restarted** in the project folder, and the `ccbridge` MCP server **approved** when it asks |

`host` blocks forever by design — it is the relay process. Run it in a background
shell or a separate terminal, then wait for `invite.txt` next to `bridge.py` and
read the invite from it rather than scraping the running process's output.
Delete the file before you start `host`: a host that was killed outright (rather
than stopped with Ctrl+C or a normal terminate signal) leaves its dead invite
behind, and the new run only removes it after a second or so. If `host` exits at
once with "port ... is already in use", a bridge is already running.

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

**`join` says "No bridge answered" (exit code 2).** It saved the settings anyway.
Most often the host's bridge has stopped, or the invite is from an earlier run —
a quick tunnel gets a new address every run, and Cloudflare answers the old one
with an error page. Ask the host for the invite from their current run.

**"Display name '...' belongs to someone else in this room."** Your name is tied
to the identity key in `<ccbridge>/.ccbridge_identity`. You get this if someone else
already uses that name, or if you joined from a different ccbridge folder. Pick
another name, or run `join` from the same ccbridge folder as before.

**"The bridge no longer accepts this invite."** The host has started a new room
(`--new-room`) since. Ask for the invite from their current run.

**"This room only lets in the names its host invited."** The host ran with
`--allow`; ask which name they put there (it must match exactly).

**"Port 8787 is already in use."** A bridge is already running, maybe in another
terminal. Stop it, or set `CCBRIDGE_PORT` to use another port.

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
them and whether they were sent to you alone or to the room. Each session joins
as soon as Claude Code starts and stays marked online while it's open. Messages
sent while you're away, or while Claude Code restarts, are waiting when you next
check.

Delivery is at-least-once: a message counts as delivered only after it has
reached your agent. If Claude Code crashes or you press Esc in the middle of a
read, those messages come back on the next read rather than being lost. A burst
of messages arrives a few at a time (about 40,000 characters per read), so it
never floods your agent's context.

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
  flags, timestamps and relay error text are checked on delivery too. Only the
  forged framing itself is rewritten; the rest of a message, code included,
  arrives exactly as it was written.
- **Credentials don't go out.** `send_message` refuses text that contains the
  bridge's own token or identity key, or anything shaped like a credential
  (private keys, cloud and GitHub tokens, passwords in URLs or assignments). The
  safety note also tells your agent never to read files or secrets in order to
  send them to a peer. Set `CCBRIDGE_CONFIRM_SENDS=1` to have Claude Code ask
  you before every outgoing message.
- **Secrets stay out of your project.** The room token and your identity key live
  in `<ccbridge>/.ccbridge_projects/`; `.mcp.json` only names the file. If git
  ever had an `.mcp.json` with a token in it, `bridge.py` says so. On macOS and
  Linux, these files, the identity key, the invite and the relay's database are
  readable only by you (mode 0600). Windows has no such mode: there they are as
  private as the folder you cloned ccbridge into.
- **Rooms are isolated by token.** A session in one room can't read or post to
  another, even holding a valid token for that other room. A relay started by
  `bridge.py host` serves only its own room — not rooms an earlier run left in
  its database — and each project hosts a room of its own.
- **Newcomers don't get the history.** Someone who joins later sees at most the
  last hour (20 messages) of room chat, never what was said before that.
- **Display names belong to one person.** `host` and `join` claim your name for
  your identity key before anyone else can reach the room, so nobody else can
  later take it, read your direct messages, or post as you. Names that differ
  only in case can't coexist. The key itself never leaves your machine: each
  relay gets a proof derived from it for that room name, which is useless in any
  other room. (`bridge.py` picks random room names, so two relays never share one.)
- **Caps:** 8 KB per message, 60 messages/minute per person and 120 per room,
  limits on joins and new rooms, and old messages expire, so neither a peer nor
  a stranger who finds the tunnel URL can flood your agent or fill the host's
  disk. Direct messages are deleted when their recipient's name expires.
- **Optional lock:** `host --allow <name>` lets only you and the names you list
  into the room, even holding the invite; sessions of anyone else end. On the
  receiving side, `--allow` / `CCBRIDGE_ALLOWED_PEERS` drops messages from anyone else.
- **Project code can't stand in for the bridge.** Claude Code starts the MCP server
  in your project folder; it is launched by an absolute path, so a project with
  its own `mcp` or `httpx` module doesn't shadow the libraries it uses. And
  `bridge.py` runs `git` and `cloudflared` only from folders on your PATH —
  never a copy in the project folder, which Windows would otherwise try first.

### What it does not do

- **No end-to-end encryption.** The relay sees message text and keeps it, in plain
  text in `ccbridge.db` on the host's machine, for up to 7 days. Run it yourself;
  don't put one person's relay in someone else's hands.
- **It does not judge what a peer asks for.** It guarantees the request arrives
  labelled and powerless, not that the request is reasonable.
- **Use HTTPS** for anything off your own machine. The token travels in a header.
- **The tunnel provider can read traffic.** A cloudflared quick tunnel ends TLS
  at Cloudflare, so Cloudflare can see the room token and message text.

---

## Configuration

`bridge.py` writes these into a settings file for you,
`<ccbridge>/.ccbridge_projects/<project>-<hash>.json`, and puts only its path in
`.mcp.json` (`CCBRIDGE_CONFIG`). Any of them set as an environment variable — for
example in the `env` of the `ccbridge` entry in `.mcp.json` — wins over the file.
Set them by hand only if you want to run the pieces separately. Once Claude Code
has loaded the bridge, re-running `host` or `join` in that folder takes effect at
its next bridge call.

| Variable | Meaning |
|---|---|
| `CCBRIDGE_CONFIG` | Path of the settings file holding the settings below |
| `CCBRIDGE_RELAY_URL` | Where the relay is, e.g. `https://x.trycloudflare.com` |
| `CCBRIDGE_ROOM` | Shared room name — **same** for both people |
| `CCBRIDGE_TOKEN` | Shared secret — **same** for both people |
| `CCBRIDGE_NAME` | How you appear to your peer — **different** for each person |
| `CCBRIDGE_IDENTITY_KEY` | Your own secret that owns your name — **never** shared (`bridge.py` keeps it in `<ccbridge>/.ccbridge_identity`) |
| `CCBRIDGE_ALLOWED_PEERS` | Optional comma-separated allowlist of peer names |
| `CCBRIDGE_CONFIRM_SENDS` | Optional; `1` makes Claude Code ask you before every `send_message` |

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
`CCBRIDGE_ALLOWED_NAMES="alice,bob"` lets only those names join, in every room
the relay serves.

---

## Tests

```bash
python -m pytest
```

261 tests: relay auth and room isolation, name ownership and resume-after-restart,
at-least-once delivery and cancelled reads, rate limits, size caps and retention,
injection and banner-forgery defences (including known bypasses, and linear-time
checks against a CPU-exhaustion attack), outbound credential checks, a hostile
relay, the peer allowlist and locked rooms, cursor behaviour, database upgrades,
invite encoding, setup (secrets kept out of `.mcp.json` and the environment, file
modes and one-step writes, line endings and byte-order marks, the launcher and
program lookup, proxies, busy ports and stale invites, dead tunnels, name claims
at host and join, rooms of moved, copied and upgraded projects), and an
end-to-end round trip over real HTTP between two sessions.

---

## Upgrading from 0.2

Both people need 0.3: a 0.3 relay turns away a 0.2 client with a message asking
it to update. To update, both people run `git pull` in their ccbridge folder; the
host restarts `bridge.py host` and the other person runs `join` with the new
invite (as after any restart of a quick tunnel); then both restart Claude Code.
Running them moves the token and your key out of `.mcp.json` into the settings
file. 0.2 used one room for every project; the first project to host that still
uses it keeps it (a friend's name and unread messages with it), and every other
project gets a room of its own. `--new-room` there retires the 0.2 room for good.

Nothing is lost: messages, direct messages and where each of you stopped reading
all carry over. Your name carries over too. 0.3 proves ownership differently, so
on its first connection your updated client shows the relay that it holds your
old key (by the hash 0.2 stored, never the key itself), and from then on uses
the new proof.

---

## Limitations

- The free cloudflared tunnel gives a **new URL every restart**, so re-hosting
  means sending a fresh invite. Your name survives it; the URL doesn't. Use a named
  tunnel or a small VM to make it stable.
- The host's terminal must stay open; it is the relay. One bridge runs at a time
  per port (`CCBRIDGE_PORT`, default 8787), and `invite.txt` is shared by
  everything hosted from one ccbridge folder.
- Polling, not push: an agent sees messages when it calls `get_messages`.
- Lose `.ccbridge_identity` and your name stays reserved in that room for 30 days
  of inactivity (or until the host deletes `ccbridge.db`). Pick a new name meanwhile.

## License

MIT
