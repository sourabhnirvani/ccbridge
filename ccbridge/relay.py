"""The relay: a small shared message bus for two people's Claude Code sessions.

Deliberately dumb. It stores and forwards text and nothing else - it never runs
commands, never touches either machine, and never grants permission for anything.
Keeping it this boring is the point: it is the one component both people expose
to the network, so it should have as little power as possible.

Rooms are trust-on-first-use. The first ``/v1/join`` for a room name fixes that
room's token; later joins must present the same token. Pre-create rooms with the
``CCBRIDGE_ROOMS`` env var ("room:token,room2:token2") if you would rather not
rely on TOFU.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from .safety import MAX_NAME_LEN, sanitize, too_large

PRESENCE_TTL_SECONDS = 90.0
RATE_LIMIT_MESSAGES = 60
RATE_LIMIT_WINDOW_SECONDS = 60.0
MAX_POLL_LIMIT = 100

_NAME_RE = re.compile(rf"^[A-Za-z0-9 _.\-]{{1,{MAX_NAME_LEN}}}$")
_ROOM_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,64}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    room        TEXT PRIMARY KEY,
    token_hash  TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    room         TEXT NOT NULL,
    display_name TEXT NOT NULL,
    joined_at    REAL NOT NULL,
    last_seen    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    room           TEXT NOT NULL,
    sender_session TEXT NOT NULL,
    sender_name    TEXT NOT NULL,
    recipient_name TEXT,
    body           TEXT NOT NULL,
    flags          TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_room_id ON messages (room, id);
"""


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class JoinRequest(BaseModel):
    room: str
    display_name: str = Field(min_length=1, max_length=MAX_NAME_LEN)


class SendRequest(BaseModel):
    text: str = Field(min_length=1)
    to: Optional[str] = Field(default=None, max_length=MAX_NAME_LEN)


@dataclass(frozen=True)
class Caller:
    """An authenticated session, resolved from the request headers."""

    session_id: str
    room: str
    display_name: str


def create_app(db_path: str = "ccbridge.db") -> FastAPI:
    app = FastAPI(title="ccbridge relay", version="0.1.0")

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    lock = threading.Lock()

    # session_id -> timestamps of recent sends, for rate limiting.
    send_times: dict[str, deque[float]] = defaultdict(deque)

    # Rooms pre-provisioned via the environment take precedence over TOFU.
    for entry in filter(None, os.getenv("CCBRIDGE_ROOMS", "").split(",")):
        room, _, token = entry.partition(":")
        room, token = room.strip(), token.strip()
        if room and token:
            with lock:
                conn.execute(
                    "INSERT OR REPLACE INTO rooms (room, token_hash, created_at) VALUES (?, ?, ?)",
                    (room, _hash_token(token), time.time()),
                )
                conn.commit()

    def _bearer(authorization: Optional[str]) -> str:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Missing bearer token")
        return authorization[7:].strip()

    def _check_room_token(room: str, token: str) -> None:
        """Validate the token for ``room``, creating the room on first use."""
        row = conn.execute("SELECT token_hash FROM rooms WHERE room = ?", (room,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO rooms (room, token_hash, created_at) VALUES (?, ?, ?)",
                (room, _hash_token(token), time.time()),
            )
            conn.commit()
            return
        if not hmac.compare_digest(row["token_hash"], _hash_token(token)):
            raise HTTPException(403, "Bad room token")

    def caller(
        authorization: Optional[str] = Header(default=None),
        x_ccbridge_room: Optional[str] = Header(default=None),
        x_ccbridge_session: Optional[str] = Header(default=None),
    ) -> Caller:
        token = _bearer(authorization)
        if not x_ccbridge_room or not x_ccbridge_session:
            raise HTTPException(401, "Missing room or session header")
        with lock:
            _check_room_token(x_ccbridge_room, token)
            row = conn.execute(
                "SELECT session_id, room, display_name FROM sessions WHERE session_id = ?",
                (x_ccbridge_session,),
            ).fetchone()
            # A session id is only valid inside the room it joined, so a token for
            # room A can never be used to act inside room B.
            if row is None or row["room"] != x_ccbridge_room:
                raise HTTPException(401, "Unknown session - join again")
            conn.execute(
                "UPDATE sessions SET last_seen = ? WHERE session_id = ?",
                (time.time(), x_ccbridge_session),
            )
            conn.commit()
        return Caller(row["session_id"], row["room"], row["display_name"])

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "version": "0.1.0"}

    @app.post("/v1/join")
    def join(req: JoinRequest, authorization: Optional[str] = Header(default=None)) -> dict:
        token = _bearer(authorization)
        if not _ROOM_RE.match(req.room):
            raise HTTPException(400, "Invalid room name")
        if not _NAME_RE.match(req.display_name):
            raise HTTPException(400, "Invalid display name")

        now = time.time()
        with lock:
            _check_room_token(req.room, token)

            # Refuse a display name another *live* session in this room is using,
            # so neither person's agent can be impersonated by the other side.
            clash = conn.execute(
                "SELECT 1 FROM sessions WHERE room = ? AND display_name = ? AND last_seen > ?",
                (req.room, req.display_name, now - PRESENCE_TTL_SECONDS),
            ).fetchone()
            if clash:
                raise HTTPException(409, f"Display name '{req.display_name}' is already active in this room")

            session_id = secrets.token_urlsafe(24)
            conn.execute(
                "INSERT INTO sessions (session_id, room, display_name, joined_at, last_seen)"
                " VALUES (?, ?, ?, ?, ?)",
                (session_id, req.room, req.display_name, now, now),
            )
            cursor_row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS m FROM messages WHERE room = ?", (req.room,)
            ).fetchone()
            conn.commit()

        # Joining starts the cursor at "now" so a new session is not flooded with
        # backlog it has no context for.
        return {"session_id": session_id, "room": req.room,
                "display_name": req.display_name, "cursor": cursor_row["m"]}

    @app.post("/v1/heartbeat")
    def heartbeat(who: Caller = Depends(caller)) -> dict:
        return {"ok": True, "display_name": who.display_name}

    @app.get("/v1/peers")
    def peers(who: Caller = Depends(caller)) -> dict:
        cutoff = time.time() - PRESENCE_TTL_SECONDS
        with lock:
            rows = conn.execute(
                "SELECT display_name, joined_at, last_seen FROM sessions"
                " WHERE room = ? AND session_id != ? AND last_seen > ?"
                " ORDER BY display_name",
                (who.room, who.session_id, cutoff),
            ).fetchall()
        return {"room": who.room, "you": who.display_name,
                "peers": [dict(r) for r in rows]}

    @app.post("/v1/send")
    def send(req: SendRequest, who: Caller = Depends(caller)) -> dict:
        if too_large(req.text):
            raise HTTPException(413, "Message too large")
        if req.to is not None and not _NAME_RE.match(req.to):
            raise HTTPException(400, "Invalid recipient name")

        now = time.time()
        recent = send_times[who.session_id]
        while recent and recent[0] < now - RATE_LIMIT_WINDOW_SECONDS:
            recent.popleft()
        if len(recent) >= RATE_LIMIT_MESSAGES:
            raise HTTPException(429, "Rate limit exceeded")
        recent.append(now)

        # Sanitize here so a hostile client cannot skip it; the MCP server
        # sanitizes again on delivery in case the relay itself is hostile.
        body, flags = sanitize(req.text)

        with lock:
            cur = conn.execute(
                "INSERT INTO messages (room, sender_session, sender_name, recipient_name,"
                " body, flags, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (who.room, who.session_id, who.display_name, req.to, body, ",".join(flags), now),
            )
            conn.commit()
        return {"ok": True, "id": cur.lastrowid, "flags": flags}

    @app.get("/v1/poll")
    def poll(
        since: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=MAX_POLL_LIMIT),
        who: Caller = Depends(caller),
    ) -> dict:
        with lock:
            rows = conn.execute(
                "SELECT id, sender_name, recipient_name, body, flags, created_at"
                " FROM messages WHERE room = ? AND id > ? AND sender_session != ?"
                "   AND (recipient_name IS NULL OR recipient_name = ?)"
                " ORDER BY id LIMIT ?",
                (who.room, since, who.session_id, who.display_name, limit),
            ).fetchall()
        messages = [
            {
                "id": r["id"],
                "from": r["sender_name"],
                "to": r["recipient_name"],
                "text": r["body"],
                "flags": [f for f in r["flags"].split(",") if f],
                "at": r["created_at"],
            }
            for r in rows
        ]
        cursor = messages[-1]["id"] if messages else since
        return {"messages": messages, "cursor": cursor}

    app.state.conn = conn
    return app


app = create_app(os.getenv("CCBRIDGE_DB", "ccbridge.db"))
