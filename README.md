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

### One of you hosts

```bash
git clone https://github.com/sourabhnirvani/ccbridge
cd ccbridge
pip install -r requirements.txt

winget install --id Cloudflare.cloudflared   # macOS/Linux: brew install cloudflared
python bridge.py host <yourname>
```

It prints a single invite line and keeps running. **Leave that window open — it is
the relay.**

```
============================================================
SEND THIS ONE LINE TO YOUR FRIEND:

  python bridge.py join eyJ1IjoiaHR0cHM6Ly9hYmMtZGVm... <theirname>

============================================================
```

### The other person joins

```bash
git clone https://github.com/sourabhnirvani/ccbridge
cd ccbridge
pip install -r requirements.txt

python bridge.py join <the-invite-line-they-sent-you> <yourname>
```

Send the invite privately (Signal, a password manager). It contains the room token.

### Both of you

Restart Claude Code, then ask it:

> check the bridge — who's online?

---

## Setting this up with an AI agent

Most people will hand this to Claude Code, Codex, or Cursor rather than typing it.
Paste this prompt:

> Clone https://github.com/sourabhnirvani/ccbridge, install its requirements, and
> set up the bridge. I am the **host** — run `python bridge.py host <myname>` and
> give me the invite line it prints.
> (Or: I am **joining** — run `python bridge.py join <invite> <myname>`.)
> Then tell me to restart Claude Code.

What an agent needs to know, precisely:

| | |
|---|---|
| Install | `pip install -r requirements.txt` |
| Verify | `python -m pytest` → 38 tests should pass |
| Host | `python bridge.py host <name>` — **long-running**, do not wait for it to exit; capture the `python bridge.py join ...` line from its output |
| Join | `python bridge.py join <invite> <name>` — exits immediately after writing `.mcp.json` |
| Result | `.mcp.json` is written in the **current working directory** |
| Then | Claude Code must be **restarted** to load the MCP server |

`host` blocks forever by design — it is the relay process. Run it in a background
shell or a separate terminal, read the invite line from its output, and move on.

---

## How the two agents talk

Nobody types commands. You talk to your own Claude in plain English:

- *"Check the bridge — is she online?"* → `list_peers`
- *"Tell him I finished the data loader and pushed it"* → `send_message`
- *"Any messages from her?"* → `get_messages`

Their agent does the same on their side. Messages arrive labelled with who sent
them.

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
- **Forged harness framing is neutralized.** A peer cannot smuggle a fake
  `<system-reminder>`, task notification, tool result, or a forged copy of the
  delivery banner. Those are rewritten to harmless look-alikes and the message is
  flagged so the reader treats that sender with extra suspicion.
- **Sanitizing runs twice** — on the relay (so a hostile client can't skip it) and
  again on delivery (so a hostile *relay* can't skip it either).
- **Rooms are isolated by token.** A session in one room cannot read or post to
  another, even holding a valid token for that other room.
- **Display names can't be impersonated** while the real owner is online.
- **Caps:** 8 KB per message, 60 messages/minute per session, so a peer cannot
  flood your agent's context window.
- **Optional allowlist:** `CCBRIDGE_ALLOWED_PEERS` drops messages from anyone else.

### What it does not do

- **No end-to-end encryption.** The relay sees message text. Run it yourself; don't
  put one person's relay in someone else's hands.
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
| `CCBRIDGE_ALLOWED_PEERS` | Optional comma-separated allowlist of peer names |

Running the relay directly, without `bridge.py`:

```bash
python run_relay.py                          # 127.0.0.1:8787
CCBRIDGE_HOST=0.0.0.0 python run_relay.py    # only behind HTTPS
```

Rooms are trust-on-first-use: the first join fixes the room's token. Pre-create
them instead with `CCBRIDGE_ROOMS="room:token,room2:token2"`.

---

## Tests

```bash
python -m pytest
```

38 tests: relay auth and room isolation, rate limits, size caps, injection and
banner-forgery defences, the peer allowlist, cursor behaviour, invite encoding,
and an end-to-end round trip over real HTTP between two sessions.

---

## Limitations

- The free cloudflared tunnel gives a **new URL every restart**, so re-hosting
  means sending a fresh invite. Use a named tunnel or a small VM to make it stable.
- The host's terminal must stay open; it is the relay.
- Polling, not push: an agent sees messages when it calls `get_messages`.

## License

MIT
