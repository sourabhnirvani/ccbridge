"""The relay: a small shared message bus for two people's Claude Code sessions.

Deliberately dumb. It stores and forwards text and nothing else - it never runs
commands, never touches either machine, and never grants permission for anything.
Keeping it this boring is the point: it is the one component both people expose
to the network, so it should have as little power as possible.

Rooms are trust-on-first-use by default: the first ``/v1/join`` for a room name
fixes that room's token. Pre-create rooms with ``CCBRIDGE_ROOMS``
("room:token,room2:token2") and set ``CCBRIDGE_OPEN_ROOMS=0`` to refuse any other
room - ``bridge.py host`` does exactly that.

Display names are trust-on-first-use too, but bound to a per-person identity key
rather than to whoever is online: the first join under a name fixes that name's
key, so nobody else in the room can later take the name, read its direct
messages, or post as it.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .safety import MAX_NAME_LEN, NAME_RE, ROOM_RE, sanitize, too_large

PRESENCE_TTL_SECONDS = 90.0
RATE_LIMIT_MESSAGES = 60
RATE_LIMIT_WINDOW_SECONDS = 60.0
JOIN_LIMIT_PER_ROOM = 20
ROOM_CREATE_LIMIT = 10
MAX_POLL_LIMIT = 100
MAX_BODY_BYTES = 64 * 1024

# A name's first join delivers what was said in the room shortly before it, so a
# peer's "hi, I'm online" sent a minute early is not lost - but never the whole
# history.
FIRST_JOIN_BACKLOG_SECONDS = 3600.0
FIRST_JOIN_BACKLOG_MESSAGES = 20

# Retention. The relay is exposed to the internet, so nothing may grow forever.
MAX_ROOMS = 100
MAX_NAMES_PER_ROOM = 16
MAX_MESSAGES_PER_ROOM = 5000
MESSAGE_RETENTION_SECONDS = 7 * 24 * 3600.0
SESSION_TTL_SECONDS = 24 * 3600.0
IDENTITY_TTL_SECONDS = 30 * 24 * 3600.0
EMPTY_ROOM_TTL_SECONDS = 24 * 3600.0
PRUNE_INTERVAL_SECONDS = 3600.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    room        TEXT PRIMARY KEY,
    token_hash  TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS identities (
    room         TEXT NOT NULL,
    display_name TEXT NOT NULL,
    key_hash     TEXT NOT NULL,
    delivered_id INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL,
    last_seen    REAL NOT NULL,
    PRIMARY KEY (room, display_name)
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
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages (created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_room ON sessions (room, last_seen);
"""


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


class JoinRequest(BaseModel):
    room: str = Field(max_length=64)
    display_name: str = Field(min_length=1, max_length=MAX_NAME_LEN)
    identity_key: str = Field(min_length=16, max_length=256)


class SendRequest(BaseModel):
    text: str = Field(min_length=1)
    to: Optional[str] = Field(default=None, max_length=MAX_NAME_LEN)


@dataclass(frozen=True)
class Caller:
    """An authenticated session, resolved from the request headers."""

    session_id: str
    room: str
    display_name: str


class _Window:
    """Sliding-window rate limiter keyed by an arbitrary string. Not thread-safe."""

    def __init__(self, limit: int, seconds: float) -> None:
        self.limit = limit
        self.seconds = seconds
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float) -> bool:
        recent = self.hits[key]
        while recent and recent[0] <= now - self.seconds:
            recent.popleft()
        if len(recent) >= self.limit:
            return False
        recent.append(now)
        return True

    def prune(self, now: float) -> None:
        stale = [k for k, q in self.hits.items() if not q or q[-1] <= now - self.seconds]
        for key in stale:
            del self.hits[key]


class _BodyLimit:
    """Refuse request bodies over ``limit`` bytes before anything parses them."""

    def __init__(self, app, limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []
        size = 0
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            size += len(body)
            if size > self.limit:
                await send({"type": "http.response.start", "status": 413,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body",
                            "body": b'{"detail":"Request body too large"}'})
                return
            chunks.append(body)
            more = message.get("more_body", False)

        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def create_app(db_path: str = "ccbridge.db", open_rooms: Optional[bool] = None) -> FastAPI:
    """Build the relay. ``open_rooms`` defaults to ``CCBRIDGE_OPEN_ROOMS`` (on)."""
    if open_rooms is None:
        open_rooms = _env_flag("CCBRIDGE_OPEN_ROOMS", True)

    # The relay is reachable from the internet through a tunnel; do not publish an
    # interactive API explorer alongside it.
    app = FastAPI(title="ccbridge relay", version=__version__,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(_BodyLimit, limit=MAX_BODY_BYTES)

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default echoes the rejected input back, which crashes on text
        # that cannot be encoded (a lone surrogate) and reflects whatever was sent.
        errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
                  for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    lock = threading.Lock()

    sends = _Window(RATE_LIMIT_MESSAGES, RATE_LIMIT_WINDOW_SECONDS)
    joins = _Window(JOIN_LIMIT_PER_ROOM, RATE_LIMIT_WINDOW_SECONDS)
    room_creates = _Window(ROOM_CREATE_LIMIT, RATE_LIMIT_WINDOW_SECONDS)
    last_prune = [0.0]

    # Rooms pre-provisioned via the environment take precedence over TOFU.
    provisioned: set[str] = set()
    for entry in filter(None, os.getenv("CCBRIDGE_ROOMS", "").split(",")):
        room, _, token = entry.partition(":")
        room, token = room.strip(), token.strip()
        if not room or not token:
            continue
        if not ROOM_RE.match(room):
            raise ValueError(f"CCBRIDGE_ROOMS: invalid room name {room!r}")
        provisioned.add(room)
        with lock:
            conn.execute(
                "INSERT OR REPLACE INTO rooms (room, token_hash, created_at) VALUES (?, ?, ?)",
                (room, _hash_token(token), time.time()),
            )
            conn.commit()

    def _prune(now: float) -> None:
        """Drop expired state. Caller must hold ``lock``."""
        conn.execute("DELETE FROM messages WHERE created_at < ?", (now - MESSAGE_RETENTION_SECONDS,))
        conn.execute("DELETE FROM sessions WHERE last_seen < ?", (now - SESSION_TTL_SECONDS,))
        conn.execute("DELETE FROM identities WHERE last_seen < ?", (now - IDENTITY_TTL_SECONDS,))
        empty = conn.execute(
            "SELECT room FROM rooms WHERE created_at < ?"
            " AND room NOT IN (SELECT DISTINCT room FROM identities)",
            (now - EMPTY_ROOM_TTL_SECONDS,),
        ).fetchall()
        for row in empty:
            if row["room"] not in provisioned:
                conn.execute("DELETE FROM rooms WHERE room = ?", (row["room"],))
        conn.commit()
        for window in (sends, joins, room_creates):
            window.prune(now)
        last_prune[0] = now

    def _maybe_prune(now: float) -> None:
        if now - last_prune[0] >= PRUNE_INTERVAL_SECONDS:
            _prune(now)

    with lock:
        _prune(time.time())

    def _bearer(authorization: Optional[str]) -> str:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Missing bearer token")
        return authorization[7:].strip()

    def _check_room_token(room: str, token: str, *, create: bool, now: float) -> None:
        """Validate the token for ``room``. Only a join may create the room."""
        row = conn.execute("SELECT token_hash FROM rooms WHERE room = ?", (room,)).fetchone()
        if row is not None:
            if not hmac.compare_digest(row["token_hash"], _hash_token(token)):
                raise HTTPException(403, "Bad room token")
            return
        if not (create and open_rooms):
            raise HTTPException(403, "Unknown room")
        if conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] >= MAX_ROOMS:
            raise HTTPException(503, "Relay has no room for new rooms")
        if not room_creates.allow("*", now):
            raise HTTPException(429, "Too many new rooms - try again in a minute")
        conn.execute(
            "INSERT INTO rooms (room, token_hash, created_at) VALUES (?, ?, ?)",
            (room, _hash_token(token), now),
        )

    def _first_join_cursor(room: str, now: float) -> int:
        recent = conn.execute(
            "SELECT id FROM messages WHERE room = ? AND created_at > ? ORDER BY id DESC LIMIT ?",
            (room, now - FIRST_JOIN_BACKLOG_SECONDS, FIRST_JOIN_BACKLOG_MESSAGES),
        ).fetchall()
        if recent:
            return recent[-1]["id"] - 1
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages WHERE room = ?", (room,))
        return row.fetchone()[0]

    def caller(
        authorization: Optional[str] = Header(default=None),
        x_ccbridge_room: Optional[str] = Header(default=None),
        x_ccbridge_session: Optional[str] = Header(default=None),
    ) -> Caller:
        token = _bearer(authorization)
        if not x_ccbridge_room or not x_ccbridge_session:
            raise HTTPException(401, "Missing room or session header")
        now = time.time()
        with lock:
            _check_room_token(x_ccbridge_room, token, create=False, now=now)
            row = conn.execute(
                "SELECT session_id, room, display_name FROM sessions WHERE session_id = ?",
                (x_ccbridge_session,),
            ).fetchone()
            # A session id is only valid inside the room it joined, so a token for
            # room A can never be used to act inside room B.
            if row is None or row["room"] != x_ccbridge_room:
                raise HTTPException(401, "Unknown session - join again")
            conn.execute("UPDATE sessions SET last_seen = ? WHERE session_id = ?",
                         (now, x_ccbridge_session))
            conn.execute("UPDATE identities SET last_seen = ? WHERE room = ? AND display_name = ?",
                         (now, row["room"], row["display_name"]))
            conn.commit()
        return Caller(row["session_id"], row["room"], row["display_name"])

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "version": __version__}

    def _join_locked(req: JoinRequest, token: str, key_hash: str, now: float) -> tuple[int, str]:
        """Body of ``join``. Caller must hold ``lock``."""
        _check_room_token(req.room, token, create=True, now=now)
        if not joins.allow(req.room, now):
            raise HTTPException(429, "Too many joins - try again in a minute")

        ident = conn.execute(
            "SELECT key_hash, delivered_id FROM identities WHERE room = ? AND display_name = ?",
            (req.room, req.display_name),
        ).fetchone()
        if ident is None:
            taken = conn.execute("SELECT COUNT(*) FROM identities WHERE room = ?", (req.room,))
            if taken.fetchone()[0] >= MAX_NAMES_PER_ROOM:
                raise HTTPException(403, "This room already has the maximum number of people")
            cursor = _first_join_cursor(req.room, now)
            conn.execute(
                "INSERT INTO identities (room, display_name, key_hash, delivered_id,"
                " created_at, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
                (req.room, req.display_name, key_hash, cursor, now, now),
            )
        elif hmac.compare_digest(ident["key_hash"], key_hash):
            # The same person again - a restarted Claude Code or a second
            # window. Resume from the last message delivered to this name.
            cursor = ident["delivered_id"]
        else:
            raise HTTPException(
                409,
                f"Display name '{req.display_name}' belongs to someone else in this room."
                " Pick a different name.",
            )

        session_id = secrets.token_urlsafe(24)
        conn.execute(
            "INSERT INTO sessions (session_id, room, display_name, joined_at, last_seen)"
            " VALUES (?, ?, ?, ?, ?)",
            (session_id, req.room, req.display_name, now, now),
        )
        conn.commit()
        return cursor, session_id

    @app.post("/v1/join")
    def join(req: JoinRequest, authorization: Optional[str] = Header(default=None)) -> dict:
        token = _bearer(authorization)
        if not ROOM_RE.match(req.room):
            raise HTTPException(400, "Invalid room name")
        if not NAME_RE.match(req.display_name):
            raise HTTPException(400, "Invalid display name")

        now = time.time()
        key_hash = _hash_token(req.identity_key)
        with lock:
            try:
                cursor, session_id = _join_locked(req, token, key_hash, now)
            except HTTPException:
                conn.rollback()  # e.g. a room row created just before a 429
                raise

        return {"session_id": session_id, "room": req.room,
                "display_name": req.display_name, "cursor": cursor}

    @app.post("/v1/heartbeat")
    def heartbeat(who: Caller = Depends(caller)) -> dict:
        return {"ok": True, "display_name": who.display_name}

    @app.get("/v1/peers")
    def peers(who: Caller = Depends(caller)) -> dict:
        cutoff = time.time() - PRESENCE_TTL_SECONDS
        with lock:
            rows = conn.execute(
                "SELECT display_name, MIN(joined_at) AS joined_at, MAX(last_seen) AS last_seen"
                " FROM sessions WHERE room = ? AND display_name != ? AND last_seen > ?"
                " GROUP BY display_name ORDER BY display_name",
                (who.room, who.display_name, cutoff),
            ).fetchall()
        return {"room": who.room, "you": who.display_name,
                "peers": [dict(r) for r in rows]}

    @app.post("/v1/send")
    def send(req: SendRequest, who: Caller = Depends(caller)) -> dict:
        if too_large(req.text):
            raise HTTPException(413, "Message too large")
        if req.to is not None and not NAME_RE.match(req.to):
            raise HTTPException(400, "Invalid recipient name")
        if req.to == who.display_name:
            raise HTTPException(400, "You cannot send a message to yourself")

        # Sanitize here so a hostile client cannot skip it; the MCP server
        # sanitizes again on delivery in case the relay itself is hostile.
        body, flags = sanitize(req.text)
        if too_large(body):
            raise HTTPException(413, "Message too large after safety filtering")

        now = time.time()
        with lock:
            if req.to is not None:
                known = conn.execute(
                    "SELECT 1 FROM identities WHERE room = ? AND display_name = ?",
                    (who.room, req.to),
                ).fetchone()
                if not known:
                    raise HTTPException(404, f"No one named '{req.to}' has joined this room")
            # Limit per person, not per session, so rejoining does not reset it.
            if not sends.allow(f"{who.room}\0{who.display_name}", now):
                raise HTTPException(429, "Rate limit exceeded")
            cur = conn.execute(
                "INSERT INTO messages (room, sender_session, sender_name, recipient_name,"
                " body, flags, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (who.room, who.session_id, who.display_name, req.to, body, ",".join(flags), now),
            )
            # Keep only the newest MAX_MESSAGES_PER_ROOM messages in this room.
            conn.execute(
                "DELETE FROM messages WHERE room = ? AND id <= (SELECT id FROM messages"
                " WHERE room = ? ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (who.room, who.room, MAX_MESSAGES_PER_ROOM),
            )
            _maybe_prune(now)
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
                " FROM messages WHERE room = ? AND id > ? AND sender_name != ?"
                "   AND (recipient_name IS NULL OR recipient_name = ?)"
                " ORDER BY id LIMIT ?",
                (who.room, since, who.display_name, who.display_name, limit + 1),
            ).fetchall()
            more = len(rows) > limit
            rows = rows[:limit]
            if rows:
                conn.execute(
                    "UPDATE identities SET delivered_id = MAX(delivered_id, ?)"
                    " WHERE room = ? AND display_name = ?",
                    (rows[-1]["id"], who.room, who.display_name),
                )
                conn.commit()
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
        return {"messages": messages, "cursor": cursor, "more": more}

    app.state.conn = conn
    app.state.lock = lock

    def prune_now(now: Optional[float] = None) -> None:
        with lock:
            _prune(time.time() if now is None else now)

    app.state.prune = prune_now
    return app


def app_from_env() -> FastAPI:
    """uvicorn factory: ``uvicorn ccbridge.relay:app_from_env --factory``."""
    return create_app(os.getenv("CCBRIDGE_DB", "ccbridge.db"))
