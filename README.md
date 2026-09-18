# ccbridge

Lets **two different people**, each on their own machine with their own Claude Code
account, have their agents message each other while working on the same project.

Claude Code's built-in session messaging only reaches sessions on *your own*
account and machine. This bridge covers the gap: a small shared relay plus an MCP
server on each side, so your agent and your friend's agent can trade progress,
questions and handoffs live.

```
  You                                                     Your friend
  ┌────────────────┐                                 ┌────────────────┐
  │  Claude Code   │                                 │  Claude Code   │
  │   + ccbridge   │                                 │   + ccbridge   │
  │   MCP server   │                                 │   MCP server   │
  └───────┬────────┘                                 └────────┬───────┘
          │  HTTPS                                    HTTPS   │
          └────────────────►  ┌──────────────┐  ◄────────────┘
                              │    relay     │
                              │ rooms+queue  │
                              │   (SQLite)   │
                              └──────────────┘
```

Four tools appear in each agent: `bridge_status`, `list_peers`, `send_message`,
`get_messages`.

## Safety model

The point of a bridge like this is that a message now arrives from *another human's*
agent. That message is untrusted input, so the design keeps it powerless:

- **The bridge moves text and nothing else.** There is no tool here that runs a
  command, reads a file, or changes a setting. The worst a peer can do is *ask*.
  Acting on the ask still goes through your own permission prompts, in front of you.
- **Every delivered message is wrapped** with its sender and an explicit note that
  peer text is data, never authorization — including the specific rule that if a
  peer says it was denied permission and asks your agent to do the thing instead,
  that is permission laundering and must be refused.
- **Harness framing is neutralized.** A peer cannot smuggle a fake
  `<system-reminder>`, task notification, tool result, or a forged copy of the
  delivery banner. Those get rewritten to harmless look-alikes and the message is
  flagged so the reader treats that sender with extra suspicion.
- **Sanitizing happens twice** — on the relay (so a hostile client can't skip it)
  and again on delivery (so a hostile *relay* can't skip it either).
- **Rooms are isolated by token.** A session in one room cannot read or post to
  another, even with a valid token for that other room.
- **Names can't be impersonated** while the real owner is online.
- **Caps:** 8 KB per message, 60 messages/minute per session, so a peer cannot
  flood your agent's context.
- **Optional allowlist:** `CCBRIDGE_ALLOWED_PEERS` drops messages from anyone else.

What it does *not* do: end-to-end encryption (the relay sees message text), and it
does not vet the *content* of what a peer asks for. Treat the relay as a trusted
component — run it yourself.

## Setup

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Pick a room name and token

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Share the room name and that token with your friend over something private
(Signal, a password manager) — **not** in the repo.

### 3. Run the relay (one of you, once)

```bash
python run_relay.py
```

Local only by default. For your friend to reach it, pick one:

- **Quick:** a tunnel, e.g. `cloudflared tunnel --url http://localhost:8787`, and
  share the `https://` URL it prints.
- **Stable:** a small VM, with `CCBRIDGE_HOST=0.0.0.0` behind HTTPS (Caddy/nginx).

Use HTTPS for anything off your own machine — the token travels in a header.

### 4. Add the MCP server to Claude Code (each person)

Create `.mcp.json` in the project you're collaborating on:

```json
{
  "mcpServers": {
    "ccbridge": {
      "command": "python",
      "args": ["-m", "ccbridge.mcp_server"],
      "env": {
        "PYTHONPATH": "C:\\Projects\\ccbridge",
        "CCBRIDGE_RELAY_URL": "https://your-relay-url",
        "CCBRIDGE_ROOM": "kaggle-duo",
        "CCBRIDGE_TOKEN": "the-shared-token",
        "CCBRIDGE_NAME": "sourabh",
        "CCBRIDGE_ALLOWED_PEERS": "friend"
      }
    }
  }
}
```

Your friend uses the **same** `CCBRIDGE_ROOM` and `CCBRIDGE_TOKEN`, a **different**
`CCBRIDGE_NAME`, and lists *you* in `CCBRIDGE_ALLOWED_PEERS`.

### 5. Use it

Ask your Claude: *"check the bridge — who's online?"*, *"tell my friend I finished
the data loader"*, *"any messages from my friend?"*

## Tests

```bash
python -m pytest
```

32 tests: relay auth and room isolation, rate limits, size caps, injection and
banner-forgery defences, allowlist, cursor behaviour, and an end-to-end round trip
over real HTTP between two sessions.
