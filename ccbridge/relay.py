"""The relay: a small shared message bus for many people's AI coding agents.

Deliberately dumb. It stores and forwards text (and, on request, files people
chose to share) and nothing else - it never runs commands, never touches anyone's
machine, and never grants permission for anything. Keeping it this boring is the
point: it is the one component everyone exposes to the network, so it should
have as little power as possible.

Rooms are trust-on-first-use by default: the first ``/v1/join`` for a room name
fixes that room's token. Pre-create rooms with ``CCBRIDGE_ROOMS``
("room:token,room2:token2") and set ``CCBRIDGE_OPEN_ROOMS=0`` to refuse any other
room - ``bridge.py host`` does exactly that.

Display names are trust-on-first-use too, but bound to a per-person identity key
rather than to whoever is online: the first join under a name fixes that name's
key, so nobody else in the room can later take the name, read its direct
messages, or post as it.

Delivery is at-least-once. A poll does not mark what it returns as delivered;
the *next* poll's ``since`` (or a heartbeat's ``ack``) does. A client that
crashes or is cancelled mid-read gets the same messages again after a restart
instead of losing them.

0.4 adds what a room of hundreds or thousands of agents needs to work together:
channels (so a broadcast does not reach everyone), replies, a long-poll ``wait``
(an agent can wait for an answer instead of polling in a loop), presence with
paging, each agent's client kind and status line, a shared task board, file
sharing, and advisory file reservations so two agents do not edit the same file
at once. All of it is coordination data: the relay still executes nothing.
"""

from __future__ import annotations

import asyncio
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
from typing import Iterable, Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import __version__
from .safety import (CHANNEL_RE, CLIENT_RE, MAX_NAME_LEN, NAME_RE, ROOM_RE, normalize_reserved_path,
                     one_line, reserved_paths_overlap, safe_file_name, sanitize, too_large)

PRESENCE_TTL_SECONDS = 90.0
# Presence is written at most this often per session. Every request would
# otherwise be a database write, which is what limits a relay with thousands of
# agents polling it.
PRESENCE_WRITE_SECONDS = 15.0
RATE_LIMIT_MESSAGES = 60
# Room-wide, because one token holder can join under several names. Sized for
# big rooms; a two-person room never gets near it.
ROOM_RATE_LIMIT_MESSAGES = 3000
RATE_LIMIT_WINDOW_SECONDS = 60.0
# SQLite integers are signed 64-bit; anything larger is a 500, not a cursor.
MAX_ID = 2**63 - 1
# Joins per room per minute. High enough that a thousand agents starting at once
# get in within a couple of minutes; the member cap is what bounds the room.
JOIN_LIMIT_PER_ROOM = 600
ROOM_CREATE_LIMIT = 10
MAX_POLL_LIMIT = 100
MAX_BODY_BYTES = 64 * 1024
# How long one poll may wait for a message. Kept well under the 60 s tool
# timeout several MCP clients default to.
MAX_WAIT_SECONDS = 30.0

# A name's first join delivers what was said in the room shortly before it, so a
# peer's "hi, I'm online" sent a minute early is not lost - but never the whole
# history.
FIRST_JOIN_BACKLOG_SECONDS = 3600.0
FIRST_JOIN_BACKLOG_MESSAGES = 20

# Retention. The relay is exposed to the internet, so nothing may grow forever.
MAX_ROOMS = 1000
MAX_NAMES_PER_ROOM = 1000
MAX_MESSAGES_PER_ROOM = 50000
MESSAGE_RETENTION_SECONDS = 7 * 24 * 3600.0
SESSION_TTL_SECONDS = 24 * 3600.0
IDENTITY_TTL_SECONDS = 30 * 24 * 3600.0
EMPTY_ROOM_TTL_SECONDS = 24 * 3600.0
PRUNE_INTERVAL_SECONDS = 3600.0

MAX_PEERS_PAGE = 500
MAX_STATUS_CHARS = 200
MAX_CHANNELS_PER_NAME = 64
MAX_CHANNELS_PER_ROOM = 1000

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_ROOM_FILE_BYTES = 512 * 1024 * 1024
MAX_FILE_NOTE_CHARS = 2000

MAX_TASK_TITLE_CHARS = 200
MAX_TASKS_PER_ROOM = 20000
TASK_STATUSES = ("open", "claimed", "in_progress", "done", "failed", "cancelled")
ACTIVE_TASK_STATUSES = ("open", "claimed", "in_progress")

MAX_RESERVATION_PATHS = 50
MAX_RESERVATIONS_PER_ROOM = 10000
DEFAULT_RESERVATION_SECONDS = 3600
MAX_RESERVATION_SECONDS = 24 * 3600
MAX_REASON_CHARS = 200

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
    first_id     INTEGER NOT NULL DEFAULT 0,
    legacy       INTEGER NOT NULL DEFAULT 0,
    client       TEXT,
    status       TEXT,
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
    created_at     REAL NOT NULL,
    client_id      TEXT
);
CREATE TABLE IF NOT EXISTS subscriptions (
    room         TEXT NOT NULL,
    display_name TEXT NOT NULL,
    channel      TEXT NOT NULL,
    PRIMARY KEY (room, display_name, channel)
);
CREATE TABLE IF NOT EXISTS files (
    id             TEXT PRIMARY KEY,
    room           TEXT NOT NULL,
    sender_name    TEXT NOT NULL,
    recipient_name TEXT,
    channel        TEXT,
    name           TEXT NOT NULL,
    size           INTEGER NOT NULL,
    sha256         TEXT NOT NULL,
    note           TEXT NOT NULL DEFAULT '',
    data           BLOB NOT NULL,
    created_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room        TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL,
    assignee    TEXT,
    channel     TEXT,
    status      TEXT NOT NULL,
    claimed_by  TEXT,
    result      TEXT,
    flags       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    client_id   TEXT
);
CREATE TABLE IF NOT EXISTS reservations (
    room        TEXT NOT NULL,
    path        TEXT NOT NULL,
    holder      TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    exclusive   INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    PRIMARY KEY (room, path, holder)
);
CREATE INDEX IF NOT EXISTS idx_messages_room_id ON messages (room, id);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages (created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_room ON sessions (room, last_seen);
CREATE INDEX IF NOT EXISTS idx_files_room ON files (room, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_room ON tasks (room, status, id);
CREATE INDEX IF NOT EXISTS idx_reservations_room ON reservations (room, expires_at);
"""

# Created after _migrate(), because an older database lacks these columns.
_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_client
    ON messages (room, sender_name, client_id) WHERE client_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_client
    ON tasks (room, created_by, client_id) WHERE client_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages (room, recipient_name, id);
"""


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a database written by an older ccbridge up to date."""
    messages = _columns(conn, "messages")
    if "client_id" not in messages:
        conn.execute("ALTER TABLE messages ADD COLUMN client_id TEXT")
    for column in ("channel TEXT", "reply_to INTEGER", "kind TEXT", "ref TEXT"):
        if column.split()[0] not in messages:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {column}")
    columns = _columns(conn, "identities")
    for column in ("client TEXT", "status TEXT"):
        if column.split()[0] not in columns:
            conn.execute(f"ALTER TABLE identities ADD COLUMN {column}")
    if "legacy" not in columns:
        conn.execute("ALTER TABLE identities ADD COLUMN legacy INTEGER NOT NULL DEFAULT 0")
    if "first_id" not in columns:
        conn.execute("ALTER TABLE identities ADD COLUMN first_id INTEGER NOT NULL DEFAULT 0")
        # 0.2 stored a hash of each person's raw key; 0.3 clients prove ownership
        # with a per-room derivation of it instead. Keep the old claims - with
        # their read positions, so nothing unread is lost - and let each owner's
        # first 0.3 join take theirs over (see _join_locked).
        conn.execute("UPDATE identities SET legacy = 1")
        # 0.2 sessions would let a 0.2 client keep going without ever joining,
        # past the request to update. Make everyone join again.
        conn.execute("DELETE FROM sessions")
    conn.commit()


# 1: the client sent its raw identity key (ccbridge 0.2). 2: it sends a proof
# derived per room (0.3+). The relay cannot tell the two apart by looking, so
# the client says which; a 0.2 client is turned away with a request to update,
# rather than being allowed to claim a name its updated self could not use.
IDENTITY_SCHEME = 2

# Sent with a 409 when the name is a claim carried over from 0.2: the client may
# then prove it is the same person by sending the hash 0.2 stored.
LEGACY_CLAIM_HEADER = "X-CCBridge-Legacy-Claim"

_CLIENT_ID = dict(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class JoinRequest(BaseModel):
    room: str = Field(max_length=64)
    display_name: str = Field(min_length=1, max_length=MAX_NAME_LEN)
    identity_key: str = Field(min_length=16, max_length=256)
    identity_scheme: int = Field(default=1, ge=1, le=100)
    # Only to take over a 0.2 claim: SHA-256 of the raw identity key.
    legacy_key_hash: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    # Claim (or check) the name without starting a session: bridge.py does this
    # at setup, so the name is yours before your agent even starts, and you do
    # not show as online until it has.
    register_only: bool = False
    # What kind of agent this is (claude-code, codex, cursor, ...), shown to peers.
    client: Optional[str] = Field(default=None, max_length=32)


class SendRequest(BaseModel):
    text: str = Field(min_length=1)
    to: Optional[str] = Field(default=None, max_length=MAX_NAME_LEN)
    # Chosen by the client and reused when it retries, so a send that timed out
    # after the relay stored it is not stored twice.
    id: Optional[str] = Field(default=None, **_CLIENT_ID)
    channel: Optional[str] = Field(default=None, max_length=32)
    reply_to: Optional[int] = Field(default=None, ge=1, le=MAX_ID)


class InviteRequest(BaseModel):
    code: str = Field(min_length=1, max_length=32)


INVITE_GUESSES_PER_MINUTE = 30
_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O, U


def _normalize_code(raw: object) -> str:
    """'7q4k m9xp', '7Q4K-M9XP' and '7Q4KM9XP' are the same code; O/I/L read as 0/1/1."""
    if not isinstance(raw, str):
        return ""
    s = raw.upper().replace("O", "0").replace("I", "1").replace("L", "1")
    s = "".join(ch for ch in s if ch in _CODE_ALPHABET)
    return f"{s[:4]}-{s[4:]}" if len(s) == 8 else ""


def make_invite_code() -> str:
    """A fresh join code, XXXX-XXXX: 40 random bits."""
    return _normalize_code("".join(secrets.choice(_CODE_ALPHABET) for _ in range(8)))


JOIN_PAGE = """You have been invited to a ccbridge room.

ccbridge lets AI coding agents (Claude Code, Codex, Cursor, Antigravity, Grok,
Gemini, VS Code, Kiro, OpenCode, ...) on different machines talk, share files
and hand each other tasks. Everyone who joins this room can reach everyone else.

This link is the key to the room - share it only with people you want in.

To join (or paste this whole page to your AI agent and ask it to do it):

  1. git clone https://github.com/sourabhnirvani/ccbridge
     pip install -r ccbridge/requirements.txt
  2. In the project folder you work in, run:
     python <path-to>/ccbridge/bridge.py join {link} <your-name>
     (add --client codex, cursor, antigravity, grok, ... for a tool other than Claude Code)
  3. Restart your agent in that folder and approve the 'ccbridge' MCP server.
     Then ask it: "check the bridge - who is online?"
"""


class StatusRequest(BaseModel):
    status: str = Field(default="", max_length=4 * MAX_STATUS_CHARS)


class ChannelRequest(BaseModel):
    channel: str = Field(max_length=32)
    action: Literal["join", "leave"] = "join"


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=4 * MAX_TASK_TITLE_CHARS)
    body: str = Field(default="")
    assignee: Optional[str] = Field(default=None, max_length=MAX_NAME_LEN)
    channel: Optional[str] = Field(default=None, max_length=32)
    id: Optional[str] = Field(default=None, **_CLIENT_ID)


class TaskUpdate(BaseModel):
    action: Literal["claim", "start", "done", "fail", "release", "cancel", "comment"]
    note: str = Field(default="")


class ReserveRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=MAX_RESERVATION_PATHS)
    reason: str = Field(default="", max_length=4 * MAX_REASON_CHARS)
    seconds: int = Field(default=DEFAULT_RESERVATION_SECONDS, ge=60, le=MAX_RESERVATION_SECONDS)
    exclusive: bool = True


class ReleaseRequest(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=MAX_RESERVATION_PATHS)
    all: bool = False


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
    """Refuse request bodies over the limit before anything parses them.

    ``limits`` maps (method, path) to a bigger limit for that one route - the
    file upload - while everything else keeps the small default.
    """

    def __init__(self, app, limit: int, limits: Optional[dict] = None) -> None:
        self.app = app
        self.limit = limit
        self.limits = limits or {}

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limits.get((scope.get("method"), scope.get("path")), self.limit)
        chunks: list[bytes] = []
        size = 0
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            size += len(body)
            if size > limit:
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


def _env_int(name: str) -> Optional[int]:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _env_rooms() -> dict[str, str]:
    rooms = {}
    for entry in filter(None, os.getenv("CCBRIDGE_ROOMS", "").split(",")):
        room, _, token = entry.partition(":")
        if room.strip() and token.strip():
            rooms[room.strip()] = token.strip()
    return rooms


def _flags(raw: str) -> list[str]:
    return [f for f in raw.split(",") if f]


def create_app(db_path: str = "ccbridge.db", open_rooms: Optional[bool] = None,
               rooms: Optional[dict[str, str]] = None,
               allowed_names: Optional[Iterable[str]] = None, *,
               max_members: Optional[int] = None, max_rooms: Optional[int] = None,
               max_file_bytes: Optional[int] = None,
               invite_codes: Optional[dict[str, tuple[str, str]]] = None) -> FastAPI:
    """Build the relay.

    Each setting left as None comes from the environment: ``open_rooms`` from
    CCBRIDGE_OPEN_ROOMS (on), ``rooms`` ({room: token}, pre-provisioned) from
    CCBRIDGE_ROOMS, ``allowed_names`` from CCBRIDGE_ALLOWED_NAMES, and the sizes
    from CCBRIDGE_MAX_MEMBERS, CCBRIDGE_MAX_ROOMS and CCBRIDGE_MAX_FILE_BYTES
    (else the module constants). bridge.py passes them in, so the room token
    never sits in the environment that cloudflared and git inherit.
    """
    if open_rooms is None:
        open_rooms = _env_flag("CCBRIDGE_OPEN_ROOMS", True)
    if rooms is None:
        rooms = _env_rooms()
    if allowed_names is None:
        allowed_names = os.getenv("CCBRIDGE_ALLOWED_NAMES", "").split(",")
    # Limits are read once, here, so a test (or an embedding program) that sets
    # the module constants before calling create_app gets them for this app.
    max_members = max_members or _env_int("CCBRIDGE_MAX_MEMBERS") or MAX_NAMES_PER_ROOM
    max_rooms = max_rooms or _env_int("CCBRIDGE_MAX_ROOMS") or MAX_ROOMS
    max_file_bytes = max_file_bytes or _env_int("CCBRIDGE_MAX_FILE_BYTES") or MAX_FILE_BYTES

    # The relay is reachable from the internet through a tunnel; do not publish an
    # interactive API explorer alongside it.
    app = FastAPI(title="ccbridge relay", version=__version__,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(_BodyLimit, limit=MAX_BODY_BYTES,
                       limits={("POST", "/v1/files"): max_file_bytes})

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default echoes the rejected input back, which crashes on text
        # that cannot be encoded (a lone surrogate) and reflects whatever was sent.
        errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
                  for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if db_path != ":memory:":
        # A crash mid-write cannot corrupt a WAL database, and readers never
        # wait on the writer.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.executescript(_INDEXES)
    conn.commit()
    lock = threading.Lock()

    sends = _Window(RATE_LIMIT_MESSAGES, RATE_LIMIT_WINDOW_SECONDS)
    # Room-wide send budget grows with the room: a two-person room keeps the
    # old 120 a minute, a room of a thousand gets ROOM_RATE_LIMIT_MESSAGES.
    room_sends = _Window(min(ROOM_RATE_LIMIT_MESSAGES, max(120, 3 * max_members)),
                         RATE_LIMIT_WINDOW_SECONDS)
    # A room may fill up all at once (a fleet of agents starting together), and
    # each may rejoin once after a relay restart.
    joins = _Window(max(JOIN_LIMIT_PER_ROOM, 2 * max_members), RATE_LIMIT_WINDOW_SECONDS)
    room_creates = _Window(ROOM_CREATE_LIMIT, RATE_LIMIT_WINDOW_SECONDS)
    last_prune = [0.0]
    # Wake-ups for waiting polls. Every stored message bumps the version of the
    # audience it is for - one name, one channel, or the whole room - and wakes
    # only the polls waiting on that audience. A direct message in a room of a
    # thousand agents wakes one of them, not all thousand.
    versions: dict[tuple, int] = defaultdict(int)
    waiters: dict[tuple, set] = defaultdict(set)
    loop_ref: list = [None]

    def _audience(room: str, to: Optional[str], channel: Optional[str]) -> tuple:
        if to:
            return (room, "@", to)
        if channel:
            return (room, "#", channel)
        return (room, "*", "")

    def _wake(key: tuple) -> None:
        """Runs on the event loop: resolve every poll waiting on ``key``."""
        for fut in waiters.pop(key, ()):
            if not fut.done():
                fut.set_result(None)

    def _bump(key: tuple) -> None:
        """Called with ``lock`` held, from any thread."""
        versions[key] += 1
        loop = loop_ref[0]
        if loop is not None and waiters.get(key):
            try:
                loop.call_soon_threadsafe(_wake, key)
            except RuntimeError:  # the loop has shut down
                pass

    # bridge.py host --allow: only these names may join, in every room this relay
    # serves (bridge.py serves exactly one). A leaked invite then lets nobody in.
    only_names = frozenset(n.strip() for n in allowed_names if n.strip()) or None

    def _check_allowed_name(name: str) -> None:
        if only_names is not None and name not in only_names:
            raise HTTPException(
                403,
                "This room only lets in the names its host invited. Ask the host"
                " which name to use.",
            )

    # Pre-provisioned rooms take precedence over TOFU.
    provisioned: set[str] = set()
    for room, token in rooms.items():
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
        cutoff = now - MESSAGE_RETENTION_SECONDS
        conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
        conn.execute("DELETE FROM files WHERE created_at < ?", (cutoff,))
        conn.execute("DELETE FROM tasks WHERE updated_at < ? AND status NOT IN (?, ?, ?)",
                     (cutoff, *ACTIVE_TASK_STATUSES))
        conn.execute("DELETE FROM reservations WHERE expires_at < ?", (now,))
        conn.execute("DELETE FROM sessions WHERE last_seen < ?", (now - SESSION_TTL_SECONDS,))
        conn.execute("DELETE FROM identities WHERE last_seen < ?", (now - IDENTITY_TTL_SECONDS,))
        # Nothing addressed to a name may outlive its owner's claim on it:
        # whoever claims the name next would read it.
        gone = ("NOT EXISTS (SELECT 1 FROM identities i WHERE i.room = {t}.room"
                " AND i.display_name = {t}.{col})")
        conn.execute("DELETE FROM messages WHERE recipient_name IS NOT NULL AND "
                     + gone.format(t="messages", col="recipient_name"))
        conn.execute("DELETE FROM files WHERE recipient_name IS NOT NULL AND "
                     + gone.format(t="files", col="recipient_name"))
        conn.execute("DELETE FROM subscriptions WHERE " + gone.format(t="subscriptions", col="display_name"))
        conn.execute("DELETE FROM reservations WHERE " + gone.format(t="reservations", col="holder"))
        empty = conn.execute(
            "SELECT room FROM rooms WHERE created_at < ?"
            " AND room NOT IN (SELECT DISTINCT room FROM identities)",
            (now - EMPTY_ROOM_TTL_SECONDS,),
        ).fetchall()
        for row in empty:
            if row["room"] not in provisioned:
                for table in ("rooms", "messages", "files", "tasks", "reservations", "subscriptions"):
                    conn.execute(f"DELETE FROM {table} WHERE room = ?", (row["room"],))
        conn.commit()
        for window in (sends, room_sends, joins, room_creates):
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
        if not open_rooms and room not in provisioned:
            # A closed relay serves exactly the rooms it was started with - not
            # whatever an earlier run left in the database, or rotating the
            # token would revoke nothing.
            raise HTTPException(403, "Unknown room")
        row = conn.execute("SELECT token_hash FROM rooms WHERE room = ?", (room,)).fetchone()
        if row is not None:
            if not hmac.compare_digest(row["token_hash"], _hash_token(token)):
                raise HTTPException(403, "Bad room token")
            return
        if not (create and open_rooms):
            raise HTTPException(403, "Unknown room")
        if conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] >= max_rooms:
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

    def _highest_id() -> int:
        row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'messages'").fetchone()
        return row[0] if row else 0

    def _ack(who: "Caller", upto: int) -> None:
        """Record that ``who`` has seen everything up to ``upto``. Caller holds ``lock``.

        Clamped to the highest id ever issued, so a bogus ack cannot make a
        restarted session skip messages that do not exist yet.
        """
        upto = min(upto, _highest_id())
        if upto > 0:
            conn.execute(
                "UPDATE identities SET delivered_id = MAX(delivered_id, ?)"
                " WHERE room = ? AND display_name = ? AND delivered_id < ?",
                (upto, who.room, who.display_name, upto),
            )
            conn.commit()

    def _known_name(room: str, name: str) -> bool:
        return conn.execute("SELECT 1 FROM identities WHERE room = ? AND display_name = ?",
                            (room, name)).fetchone() is not None

    def _check_recipient(who: "Caller", to: Optional[str]) -> None:
        """Validate a direct recipient. Caller holds ``lock``."""
        if to is None:
            return
        if not NAME_RE.match(to):
            raise HTTPException(400, "Invalid recipient name")
        if to == who.display_name:
            raise HTTPException(400, "You cannot send a message to yourself")
        if not _known_name(who.room, to):
            raise HTTPException(404, f"No one named '{to}' has joined this room")

    def _check_channel(channel: Optional[str]) -> None:
        if channel is not None and not CHANNEL_RE.match(channel):
            raise HTTPException(400, "Invalid channel name (lower case letters, digits, _ . -)")

    def _rate_limit(who: "Caller", now: float) -> None:
        """Count one send against the person and the room. Caller holds ``lock``.

        Per person (not per session, so rejoining does not reset it) and per
        room (so extra names do not help).
        """
        if not sends.allow(f"{who.room}\0{who.display_name}", now):
            raise HTTPException(429, "Rate limit exceeded")
        if not room_sends.allow(who.room, now):
            raise HTTPException(429, "Rate limit exceeded for this room")

    def _insert_message(who: "Caller", body: str, flags: Iterable[str], now: float, *,
                        to: Optional[str] = None, channel: Optional[str] = None,
                        reply_to: Optional[int] = None, kind: Optional[str] = None,
                        ref: Optional[str] = None, client_id: Optional[str] = None) -> int:
        """Store one message and trim the room. Caller holds ``lock`` and commits."""
        cur = conn.execute(
            "INSERT INTO messages (room, sender_session, sender_name, recipient_name, body, flags,"
            " created_at, client_id, channel, reply_to, kind, ref)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (who.room, who.session_id, who.display_name, to, body, ",".join(flags), now,
             client_id, None if to else channel, reply_to, kind, ref),
        )
        # Keep only the newest MAX_MESSAGES_PER_ROOM messages in this room.
        conn.execute(
            "DELETE FROM messages WHERE room = ? AND id <= (SELECT id FROM messages"
            " WHERE room = ? ORDER BY id DESC LIMIT 1 OFFSET ?)",
            (who.room, who.room, MAX_MESSAGES_PER_ROOM),
        )
        _bump(_audience(who.room, to, channel))
        return cur.lastrowid

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
                "SELECT session_id, room, display_name, last_seen FROM sessions WHERE session_id = ?",
                (x_ccbridge_session,),
            ).fetchone()
            # A session id is only valid inside the room it joined, so a token for
            # room A can never be used to act inside room B.
            if row is None or row["room"] != x_ccbridge_room:
                raise HTTPException(401, "Unknown session - join again")
            # A session from before the host locked the room ends here too.
            _check_allowed_name(row["display_name"])
            if now - row["last_seen"] >= PRESENCE_WRITE_SECONDS or now < row["last_seen"]:
                conn.execute("UPDATE sessions SET last_seen = ? WHERE session_id = ?",
                             (now, x_ccbridge_session))
                conn.execute("UPDATE identities SET last_seen = ? WHERE room = ? AND display_name = ?",
                             (now, row["room"], row["display_name"]))
                conn.commit()
        return Caller(row["session_id"], row["room"], row["display_name"])

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "version": __version__,
                "features": ["channels", "wait", "tasks", "files", "reservations", "status"]}

    def _join_locked(req: JoinRequest, token: str, key_hash: str,
                     now: float) -> tuple[int, Optional[str]]:
        """Body of ``join``. Caller must hold ``lock``."""
        _check_room_token(req.room, token, create=True, now=now)
        # After the token check, so the list of names is no oracle for anyone
        # without the token; before the join limit, so names that are not let in
        # cannot use it up and lock out the ones that are.
        _check_allowed_name(req.display_name)
        if not joins.allow(req.room, now):
            raise HTTPException(429, "Too many joins - try again in a minute")

        ident = conn.execute(
            "SELECT key_hash, delivered_id, legacy FROM identities WHERE room = ? AND display_name = ?",
            (req.room, req.display_name),
        ).fetchone()
        if ident is None:
            # "Alice" next to "alice" is an impersonation waiting to happen.
            similar = conn.execute(
                "SELECT display_name FROM identities WHERE room = ? AND lower(display_name) = lower(?)",
                (req.room, req.display_name),
            ).fetchone()
            if similar is not None:
                raise HTTPException(
                    409,
                    f"Display name '{req.display_name}' is too close to '{similar['display_name']}',"
                    " which is already in use in this room. Pick a different name.",
                )
            taken = conn.execute("SELECT COUNT(*) FROM identities WHERE room = ?", (req.room,))
            if taken.fetchone()[0] >= max_members:
                raise HTTPException(403, "This room already has the maximum number of people")
            cursor = _first_join_cursor(req.room, now)
            # first_id is a floor: this name never sees anything older, even by
            # polling with since=0.
            conn.execute(
                "INSERT INTO identities (room, display_name, key_hash, delivered_id, first_id,"
                " created_at, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (req.room, req.display_name, key_hash, cursor, cursor, now, now),
            )
        elif hmac.compare_digest(ident["key_hash"], key_hash):
            # The same person again - a restarted agent or a second window.
            # Resume from the last message delivered to this name.
            cursor = ident["delivered_id"]
        elif ident["legacy"] and req.legacy_key_hash is not None and hmac.compare_digest(
            ident["key_hash"], req.legacy_key_hash
        ):
            # The owner of a claim carried over from 0.2, proven by the hash 0.2
            # stored (only the holder of the old raw key can produce it). From
            # now on the name is held by the 0.3 proof, and reading resumes
            # exactly where it stopped before the upgrade.
            conn.execute(
                "UPDATE identities SET key_hash = ?, legacy = 0 WHERE room = ? AND display_name = ?",
                (key_hash, req.room, req.display_name),
            )
            cursor = ident["delivered_id"]
        else:
            raise HTTPException(
                409,
                f"Display name '{req.display_name}' belongs to someone else in this room."
                " Pick a different name.",
                headers={LEGACY_CLAIM_HEADER: "1"} if ident["legacy"] else None,
            )

        if req.client is not None:
            conn.execute("UPDATE identities SET client = ? WHERE room = ? AND display_name = ?",
                         (req.client, req.room, req.display_name))
        if req.register_only:
            conn.execute("UPDATE identities SET last_seen = ? WHERE room = ? AND display_name = ?",
                         (now, req.room, req.display_name))
            conn.commit()
            return cursor, None
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
        if req.identity_scheme < IDENTITY_SCHEME:
            raise HTTPException(
                426,
                f"This relay runs ccbridge {__version__}, which needs a newer client. Update"
                " ccbridge (git pull in the ccbridge folder) and restart your agent.",
            )
        if not ROOM_RE.match(req.room):
            raise HTTPException(400, "Invalid room name")
        if not NAME_RE.match(req.display_name):
            raise HTTPException(400, "Invalid display name")
        if req.client is not None and not CLIENT_RE.match(req.client):
            raise HTTPException(400, "Invalid client name")

        now = time.time()
        key_hash = _hash_token(req.identity_key)
        with lock:
            try:
                cursor, session_id = _join_locked(req, token, key_hash, now)
            except HTTPException:
                conn.rollback()  # e.g. a room row created just before a 429
                raise

        return {"session_id": session_id, "room": req.room,
                "display_name": req.display_name, "cursor": cursor, "version": __version__}

    @app.post("/v1/heartbeat")
    def heartbeat(
        ack: Optional[int] = Query(default=None, ge=0, le=MAX_ID),
        who: Caller = Depends(caller),
    ) -> dict:
        if ack is not None:
            with lock:
                _ack(who, ack)
        return {"ok": True, "display_name": who.display_name}

    @app.post("/v1/status")
    def set_status(req: StatusRequest, who: Caller = Depends(caller)) -> dict:
        status, flags = one_line(req.status, MAX_STATUS_CHARS)
        with lock:
            conn.execute("UPDATE identities SET status = ? WHERE room = ? AND display_name = ?",
                         (status or None, who.room, who.display_name))
            conn.commit()
        return {"ok": True, "status": status, "flags": flags}

    @app.get("/v1/peers")
    def peers(
        q: Optional[str] = Query(default=None, max_length=MAX_NAME_LEN),
        channel: Optional[str] = Query(default=None, max_length=32),
        limit: int = Query(default=100, ge=1, le=MAX_PEERS_PAGE),
        offset: int = Query(default=0, ge=0, le=10**6),
        include_offline: bool = False,
        who: Caller = Depends(caller),
    ) -> dict:
        _check_channel(channel)
        cutoff = time.time() - PRESENCE_TTL_SECONDS
        where = ["i.room = ?", "i.display_name != ?"]
        args: list = [who.room, who.display_name]
        if q:
            where.append("lower(i.display_name) LIKE ? ESCAPE '\\'")
            args.append("%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if channel:
            where.append("EXISTS (SELECT 1 FROM subscriptions s WHERE s.room = i.room"
                         " AND s.display_name = i.display_name AND s.channel = ?)")
            args.append(channel)
        online = ("EXISTS (SELECT 1 FROM sessions x WHERE x.room = i.room"
                  " AND x.display_name = i.display_name AND x.last_seen > ?)")
        if not include_offline:
            where.append(online)
            args.append(cutoff)
        sql_where = " AND ".join(where)
        with lock:
            total = conn.execute(f"SELECT COUNT(*) FROM identities i WHERE {sql_where}", args).fetchone()[0]
            rows = conn.execute(
                "SELECT i.display_name, i.client, i.status, i.last_seen,"
                " (SELECT MIN(joined_at) FROM sessions x WHERE x.room = i.room"
                "  AND x.display_name = i.display_name) AS joined_at,"
                f" {online} AS online"
                f" FROM identities i WHERE {sql_where} ORDER BY i.display_name LIMIT ? OFFSET ?",
                [cutoff, *args, limit, offset],
            ).fetchall()
        return {"room": who.room, "you": who.display_name, "total": total,
                "more": offset + len(rows) < total,
                "peers": [{"display_name": r["display_name"], "client": r["client"],
                           "status": r["status"], "joined_at": r["joined_at"],
                           "last_seen": r["last_seen"], "online": bool(r["online"])}
                          for r in rows]}

    # --- channels -------------------------------------------------------------

    @app.post("/v1/channels")
    def channels_change(req: ChannelRequest, who: Caller = Depends(caller)) -> dict:
        _check_channel(req.channel)
        with lock:
            if req.action == "join":
                mine = conn.execute("SELECT COUNT(*) FROM subscriptions WHERE room = ? AND display_name = ?",
                                    (who.room, who.display_name)).fetchone()[0]
                exists = conn.execute("SELECT 1 FROM subscriptions WHERE room = ? AND channel = ? LIMIT 1",
                                      (who.room, req.channel)).fetchone()
                if mine >= MAX_CHANNELS_PER_NAME:
                    raise HTTPException(403, f"You are already in {MAX_CHANNELS_PER_NAME} channels")
                if exists is None:
                    count = conn.execute("SELECT COUNT(DISTINCT channel) FROM subscriptions WHERE room = ?",
                                         (who.room,)).fetchone()[0]
                    if count >= MAX_CHANNELS_PER_ROOM:
                        raise HTTPException(403, "This room already has the maximum number of channels")
                conn.execute("INSERT OR IGNORE INTO subscriptions (room, display_name, channel)"
                             " VALUES (?, ?, ?)", (who.room, who.display_name, req.channel))
            else:
                conn.execute("DELETE FROM subscriptions WHERE room = ? AND display_name = ? AND channel = ?",
                             (who.room, who.display_name, req.channel))
            conn.commit()
            mine = [r[0] for r in conn.execute(
                "SELECT channel FROM subscriptions WHERE room = ? AND display_name = ? ORDER BY channel",
                (who.room, who.display_name))]
        return {"ok": True, "channels": mine}

    @app.get("/v1/channels")
    def channels_list(who: Caller = Depends(caller)) -> dict:
        with lock:
            rows = conn.execute(
                "SELECT channel, COUNT(*) AS members FROM subscriptions WHERE room = ?"
                " GROUP BY channel ORDER BY members DESC, channel LIMIT 500", (who.room,)).fetchall()
            mine = [r[0] for r in conn.execute(
                "SELECT channel FROM subscriptions WHERE room = ? AND display_name = ? ORDER BY channel",
                (who.room, who.display_name))]
        return {"channels": [dict(r) for r in rows], "mine": mine}

    # --- messages -------------------------------------------------------------

    @app.post("/v1/send")
    def send(req: SendRequest, who: Caller = Depends(caller)) -> dict:
        if too_large(req.text):
            raise HTTPException(413, "Message too large")
        if req.to is not None and not NAME_RE.match(req.to):
            raise HTTPException(400, "Invalid recipient name")
        if req.to == who.display_name:
            raise HTTPException(400, "You cannot send a message to yourself")
        _check_channel(req.channel)
        if req.to is not None and req.channel is not None:
            raise HTTPException(400, "A message goes either to one person or to a channel, not both")

        def _already_stored() -> Optional[dict]:
            if req.id is None:
                return None
            row = conn.execute(
                "SELECT id, flags FROM messages WHERE room = ? AND sender_name = ? AND client_id = ?",
                (who.room, who.display_name, req.id),
            ).fetchone()
            if row is None:
                return None
            return {"ok": True, "id": row["id"], "flags": _flags(row["flags"]), "duplicate": True}

        now = time.time()
        with lock:
            _check_recipient(who, req.to)
            # A retry of a send the relay already stored costs nothing.
            stored = _already_stored()
            if stored is not None:
                return stored
            # Rate limits come before sanitizing, so a flood of rejected sends
            # costs the relay no CPU.
            _rate_limit(who, now)

        # Sanitize here so a hostile client cannot skip it; the MCP server
        # sanitizes again on delivery in case the relay itself is hostile.
        body, flags = sanitize(req.text)
        if too_large(body):
            raise HTTPException(413, "Message too large after safety filtering")

        with lock:
            try:
                msg_id = _insert_message(who, body, flags, now, to=req.to, channel=req.channel,
                                         reply_to=req.reply_to, client_id=req.id)
            except sqlite3.IntegrityError:
                # Two copies of the same retry raced; the first one won.
                conn.rollback()
                stored = _already_stored()
                if stored is None:
                    raise
                return stored
            _maybe_prune(now)
            conn.commit()
        return {"ok": True, "id": msg_id, "flags": flags}

    def _poll_once(who: Caller, since: int, limit: int) -> tuple[dict, list, tuple]:
        """One poll: (response, the audiences this name hears, their versions now).

        The versions are read under the same lock as the query, so a message
        stored after the query is always seen as a version change.
        """
        with lock:
            # Asking for what comes after ``since`` says everything up to it
            # arrived. What this poll returns is acknowledged by the next one.
            _ack(who, since)
            floor = conn.execute(
                "SELECT first_id FROM identities WHERE room = ? AND display_name = ?",
                (who.room, who.display_name),
            ).fetchone()
            # Never anything from before this name joined, whatever ``since`` says.
            start = max(since, floor["first_id"] if floor else MAX_ID)
            top = _highest_id()
            rows = conn.execute(
                "SELECT id, sender_name, recipient_name, body, flags, created_at, channel,"
                " reply_to, kind, ref FROM messages"
                " WHERE room = ? AND id > ? AND sender_name != ?"
                "   AND (recipient_name = ? OR (recipient_name IS NULL AND (channel IS NULL"
                "        OR channel IN (SELECT channel FROM subscriptions"
                "                       WHERE room = ? AND display_name = ?))))"
                " ORDER BY id LIMIT ?",
                (who.room, start, who.display_name, who.display_name, who.room, who.display_name,
                 limit + 1),
            ).fetchall()
            keys = [_audience(who.room, None, None), _audience(who.room, who.display_name, None)]
            keys += [_audience(who.room, None, r[0]) for r in conn.execute(
                "SELECT channel FROM subscriptions WHERE room = ? AND display_name = ?",
                (who.room, who.display_name))]
            snapshot = tuple(versions.get(k, 0) for k in keys)
        more = len(rows) > limit
        rows = rows[:limit]
        messages = [
            {
                "id": r["id"],
                "from": r["sender_name"],
                "to": r["recipient_name"],
                "text": r["body"],
                "flags": _flags(r["flags"]),
                "at": r["created_at"],
                "channel": r["channel"],
                "reply_to": r["reply_to"],
                "kind": r["kind"] or "message",
                "ref": r["ref"],
            }
            for r in rows
        ]
        if more:
            cursor = messages[-1]["id"]
        else:
            # Everything up to ``top`` was looked at and nothing else is for this
            # name, so the next poll may start there. Without this, a quiet agent
            # in a busy room would rescan everyone else's traffic on every poll.
            cursor = max(since, top) if not messages else max(messages[-1]["id"], min(top, MAX_ID))
        return {"messages": messages, "cursor": cursor, "more": more}, keys, snapshot

    @app.get("/v1/poll")
    async def poll(
        since: int = Query(default=0, ge=0, le=MAX_ID),
        limit: int = Query(default=50, ge=1, le=MAX_POLL_LIMIT),
        wait: float = Query(default=0.0, ge=0.0, le=MAX_WAIT_SECONDS),
        who: Caller = Depends(caller),
    ) -> dict:
        """Messages for the caller after ``since``; with ``wait``, hold until one arrives.

        Waiting costs the relay no thread and no database work: the poll sleeps
        on a future that is resolved only when a message for one of its
        audiences (its name, its channels, the room) is stored.
        """
        loop = asyncio.get_running_loop()
        loop_ref[0] = loop
        deadline = loop.time() + wait
        while True:
            result, keys, snapshot = await run_in_threadpool(_poll_once, who, since, limit)
            remaining = deadline - loop.time()
            if result["messages"] or remaining <= 0:
                return result
            fut = loop.create_future()
            for key in keys:
                waiters[key].add(fut)
            try:
                # Anything stored since the query moved a version: look again now.
                if tuple(versions.get(k, 0) for k in keys) == snapshot:
                    try:
                        await asyncio.wait_for(fut, remaining)
                    except asyncio.TimeoutError:
                        return result
            finally:
                for key in keys:
                    bucket = waiters.get(key)
                    if bucket is not None:
                        bucket.discard(fut)
                        if not bucket:
                            waiters.pop(key, None)
            since = result["cursor"]

    # --- files ----------------------------------------------------------------

    def _visible_file(row, who: Caller) -> bool:
        return (row["recipient_name"] is None or row["recipient_name"] == who.display_name
                or row["sender_name"] == who.display_name)

    def _file_meta(row) -> dict:
        return {"id": row["id"], "name": row["name"], "size": row["size"], "sha256": row["sha256"],
                "from": row["sender_name"], "to": row["recipient_name"], "channel": row["channel"],
                "note": row["note"], "at": row["created_at"]}

    def _store_file(who: Caller, data: bytes, name: str, to: Optional[str], channel: Optional[str],
                    note: str) -> dict:
        now = time.time()
        safe = safe_file_name(name)
        note_text, note_flags = sanitize(note)
        note_text = note_text[:MAX_FILE_NOTE_CHARS]
        digest = hashlib.sha256(data).hexdigest()
        file_id = secrets.token_hex(8)
        with lock:
            _check_recipient(who, to)
            _rate_limit(who, now)
            used = conn.execute("SELECT COALESCE(SUM(size), 0) FROM files WHERE room = ?",
                                (who.room,)).fetchone()[0]
            if used + len(data) > MAX_ROOM_FILE_BYTES:
                raise HTTPException(507, "This room has used up its file storage; old files expire after a week")
            conn.execute(
                "INSERT INTO files (id, room, sender_name, recipient_name, channel, name, size,"
                " sha256, note, data, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (file_id, who.room, who.display_name, to, None if to else channel, safe, len(data),
                 digest, note_text, data, now),
            )
            text = (f"shared a file: {safe} ({len(data)} bytes, sha256 {digest[:16]}...)."
                    f" File id: {file_id}. Use download_file to save it.")
            if note_text.strip():
                text += "\nNote from the sender:\n" + note_text
            _insert_message(who, text, note_flags, now, to=to, channel=channel, kind="file", ref=file_id)
            _maybe_prune(now)
            conn.commit()
        return {"ok": True, "id": file_id, "name": safe, "size": len(data), "sha256": digest,
                "flags": note_flags}

    @app.post("/v1/files")
    async def upload_file(
        request: Request,
        name: str = Query(min_length=1, max_length=512),
        to: Optional[str] = Query(default=None, max_length=MAX_NAME_LEN),
        channel: Optional[str] = Query(default=None, max_length=32),
        note: str = Query(default="", max_length=4 * MAX_FILE_NOTE_CHARS),
        who: Caller = Depends(caller),
    ) -> dict:
        _check_channel(channel)
        if to is not None and channel is not None:
            raise HTTPException(400, "A file goes either to one person or to a channel, not both")
        data = await request.body()
        if not data:
            raise HTTPException(400, "Empty file")
        if len(data) > max_file_bytes:
            raise HTTPException(413, "File too large")
        return await run_in_threadpool(_store_file, who, data, name, to, channel, note)

    @app.get("/v1/files")
    def list_files(limit: int = Query(default=50, ge=1, le=200),
                   who: Caller = Depends(caller)) -> dict:
        with lock:
            rows = conn.execute(
                "SELECT id, name, size, sha256, sender_name, recipient_name, channel, note, created_at"
                " FROM files WHERE room = ? AND (recipient_name IS NULL OR recipient_name = ?"
                " OR sender_name = ?) ORDER BY created_at DESC LIMIT ?",
                (who.room, who.display_name, who.display_name, limit)).fetchall()
        return {"files": [_file_meta(r) for r in rows]}

    @app.get("/v1/files/{file_id}")
    def download_file(file_id: str, who: Caller = Depends(caller)) -> Response:
        with lock:
            row = conn.execute("SELECT * FROM files WHERE room = ? AND id = ?",
                               (who.room, file_id)).fetchone()
        if row is None or not _visible_file(row, who):
            raise HTTPException(404, "No such file (it may have expired)")
        return Response(content=bytes(row["data"]), media_type="application/octet-stream", headers={
            "X-CCBridge-File-Name": row["name"], "X-CCBridge-Sha256": row["sha256"],
            "X-CCBridge-From": row["sender_name"], "X-CCBridge-Size": str(row["size"]),
            "Content-Disposition": "attachment",
        })

    # --- tasks ----------------------------------------------------------------

    def _task_dict(row, full: bool = True) -> dict:
        out = {k: row[k] for k in ("id", "title", "created_by", "assignee", "channel", "status",
                                   "claimed_by", "created_at", "updated_at")}
        out["flags"] = _flags(row["flags"])
        if full:
            out["body"] = row["body"]
            out["result"] = row["result"]
        return out

    def _notify(who: Caller, recipients: Iterable[Optional[str]], text: str, flags: Iterable[str],
                now: float, ref: str, channel: Optional[str] = None) -> None:
        """Tell each named recipient (other than the actor) about a task event."""
        sent = set()
        for name in recipients:
            if name and name != who.display_name and name not in sent and _known_name(who.room, name):
                _insert_message(who, text, flags, now, to=name, kind="task", ref=ref)
                sent.add(name)

    @app.post("/v1/tasks")
    def create_task(req: TaskCreate, who: Caller = Depends(caller)) -> dict:
        _check_channel(req.channel)
        if too_large(req.body):
            raise HTTPException(413, "Task description too large")
        if req.assignee is not None and req.channel is not None:
            raise HTTPException(400, "A task goes either to one person or to a channel, not both")
        now = time.time()
        with lock:
            _check_recipient(who, req.assignee)
            if req.id is not None:
                row = conn.execute("SELECT * FROM tasks WHERE room = ? AND created_by = ? AND client_id = ?",
                                   (who.room, who.display_name, req.id)).fetchone()
                if row is not None:
                    return {"ok": True, "task": _task_dict(row), "duplicate": True}
            _rate_limit(who, now)
            count = conn.execute("SELECT COUNT(*) FROM tasks WHERE room = ?", (who.room,)).fetchone()[0]
            if count >= MAX_TASKS_PER_ROOM:
                raise HTTPException(507, "This room has too many tasks; finished ones expire after a week")
        title, tflags = one_line(req.title, MAX_TASK_TITLE_CHARS)
        body, bflags = sanitize(req.body)
        if not title:
            raise HTTPException(400, "A task needs a title")
        flags = sorted(set(tflags) | set(bflags))
        with lock:
            try:
                cur = conn.execute(
                    "INSERT INTO tasks (room, title, body, created_by, assignee, channel, status, flags,"
                    " created_at, updated_at, client_id) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)",
                    (who.room, title, body, who.display_name, req.assignee, req.channel,
                     ",".join(flags), now, now, req.id))
            except sqlite3.IntegrityError:
                conn.rollback()
                row = conn.execute("SELECT * FROM tasks WHERE room = ? AND created_by = ? AND client_id = ?",
                                   (who.room, who.display_name, req.id)).fetchone()
                if row is None:
                    raise
                return {"ok": True, "task": _task_dict(row), "duplicate": True}
            task_id = cur.lastrowid
            target = f"for {req.assignee}" if req.assignee else (
                f"for anyone in #{req.channel}" if req.channel else "for anyone in the room")
            text = f"created task #{task_id} {target}: {title}"
            if body.strip():
                text += "\n\n" + body
            text += f"\n\n(Claim it with update_task(task_id={task_id}, action='claim') if you will do it.)"
            _insert_message(who, text, flags, now, to=req.assignee,
                            channel=None if req.assignee else req.channel, kind="task", ref=str(task_id))
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            _maybe_prune(now)
            conn.commit()
        return {"ok": True, "task": _task_dict(row)}

    @app.get("/v1/tasks")
    def list_tasks(
        status: str = Query(default="active", pattern="^(active|all|open|claimed|in_progress|done|failed|cancelled)$"),
        mine: bool = False,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0, le=10**6),
        who: Caller = Depends(caller),
    ) -> dict:
        where = ["room = ?"]
        args: list = [who.room]
        if status == "active":
            where.append("status IN (?, ?, ?)")
            args += ACTIVE_TASK_STATUSES
        elif status != "all":
            where.append("status = ?")
            args.append(status)
        if mine:
            where.append("(created_by = ? OR assignee = ? OR claimed_by = ?)")
            args += [who.display_name] * 3
        sql_where = " AND ".join(where)
        with lock:
            total = conn.execute(f"SELECT COUNT(*) FROM tasks WHERE {sql_where}", args).fetchone()[0]
            rows = conn.execute(f"SELECT * FROM tasks WHERE {sql_where} ORDER BY id DESC LIMIT ? OFFSET ?",
                                [*args, limit, offset]).fetchall()
        return {"total": total, "more": offset + len(rows) < total,
                "tasks": [_task_dict(r, full=False) for r in rows]}

    @app.get("/v1/tasks/{task_id}")
    def get_task(task_id: int, who: Caller = Depends(caller)) -> dict:
        with lock:
            row = conn.execute("SELECT * FROM tasks WHERE room = ? AND id = ?", (who.room, task_id)).fetchone()
        if row is None:
            raise HTTPException(404, "No such task")
        return {"task": _task_dict(row)}

    @app.post("/v1/tasks/{task_id}")
    def update_task(task_id: int, req: TaskUpdate, who: Caller = Depends(caller)) -> dict:
        if too_large(req.note):
            raise HTTPException(413, "Note too large")
        me = who.display_name
        now = time.time()
        with lock:
            # Every update may message the people involved, so every update
            # counts against the sender's limits - before any sanitizing work.
            _rate_limit(who, now)
        note, flags = sanitize(req.note)
        with lock:
            row = conn.execute("SELECT * FROM tasks WHERE room = ? AND id = ?", (who.room, task_id)).fetchone()
            if row is None:
                raise HTTPException(404, "No such task")
            status, claimer, creator = row["status"], row["claimed_by"], row["created_by"]
            finished = status not in ACTIVE_TASK_STATUSES
            act = req.action
            new_status, new_claimer, result = status, claimer, row["result"]
            if act == "comment":
                if not note.strip():
                    raise HTTPException(400, "A comment needs a note")
            elif finished:
                raise HTTPException(409, f"Task #{task_id} is already {status}")
            elif act == "claim":
                if row["assignee"] and row["assignee"] != me:
                    raise HTTPException(403, f"Task #{task_id} is assigned to {row['assignee']}")
                if claimer and claimer != me:
                    raise HTTPException(409, f"Task #{task_id} was already claimed by {claimer}")
                new_status, new_claimer = ("claimed" if status == "open" else status), me
            elif act in ("start", "done", "fail", "release"):
                if claimer != me:
                    raise HTTPException(403, f"Claim task #{task_id} first" if not claimer
                                        else f"Task #{task_id} is claimed by {claimer}, not you")
                if act == "start":
                    new_status = "in_progress"
                elif act == "release":
                    new_status, new_claimer = "open", None
                else:
                    new_status, result = ("done" if act == "done" else "failed"), note
            elif act == "cancel":
                if creator != me:
                    raise HTTPException(403, f"Only {creator}, who created task #{task_id}, can cancel it")
                new_status = "cancelled"
            all_flags = sorted(set(_flags(row["flags"])) | set(flags))
            conn.execute(
                "UPDATE tasks SET status = ?, claimed_by = ?, result = ?, flags = ?, updated_at = ?"
                " WHERE id = ?", (new_status, new_claimer, result, ",".join(all_flags), now, task_id))
            verb = {"claim": "claimed", "start": "started", "done": "finished", "fail": "gave up on",
                    "release": "released", "cancel": "cancelled", "comment": "commented on"}[act]
            text = f"{verb} task #{task_id}: {row['title']} (status: {new_status})"
            if note.strip():
                text += "\n\n" + note
            _notify(who, [creator, claimer, new_claimer, row["assignee"]], text, flags, now, str(task_id))
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            conn.commit()
        return {"ok": True, "task": _task_dict(row)}

    # --- reservations ---------------------------------------------------------

    def _active_reservations(room: str, now: float) -> list:
        return conn.execute(
            "SELECT path, holder, reason, exclusive, created_at, expires_at FROM reservations"
            " WHERE room = ? AND expires_at > ? ORDER BY path", (room, now)).fetchall()

    @app.post("/v1/reservations")
    def reserve(req: ReserveRequest, who: Caller = Depends(caller)) -> dict:
        paths = []
        for raw in req.paths:
            p = normalize_reserved_path(raw)
            if p is None:
                raise HTTPException(400, f"Not a usable project-relative path: {raw[:80]!r}")
            if p not in paths:
                paths.append(p)
        reason, _ = one_line(req.reason, MAX_REASON_CHARS)
        now = time.time()
        with lock:
            active = _active_reservations(who.room, now)
            conflicts = []
            for p in paths:
                for r in active:
                    if r["holder"] == who.display_name:
                        continue
                    if not (req.exclusive or r["exclusive"]):
                        continue  # two shared reservations never conflict
                    if reserved_paths_overlap(p, r["path"]):
                        conflicts.append({"path": p, "held": r["path"], "holder": r["holder"],
                                          "reason": r["reason"], "expires_at": r["expires_at"]})
            if conflicts:
                return {"ok": False, "granted": [], "conflicts": conflicts}
            mine = {r["path"] for r in active if r["holder"] == who.display_name}
            if len(active) + len([p for p in paths if p not in mine]) > MAX_RESERVATIONS_PER_ROOM:
                raise HTTPException(507, "This room has too many reservations")
            for p in paths:
                conn.execute(
                    "INSERT OR REPLACE INTO reservations (room, path, holder, reason, exclusive,"
                    " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (who.room, p, who.display_name, reason, int(req.exclusive), now, now + req.seconds))
            conn.commit()
        return {"ok": True, "granted": paths, "expires_at": now + req.seconds, "conflicts": []}

    @app.post("/v1/reservations/release")
    def release(req: ReleaseRequest, who: Caller = Depends(caller)) -> dict:
        with lock:
            if req.all:
                cur = conn.execute("DELETE FROM reservations WHERE room = ? AND holder = ?",
                                   (who.room, who.display_name))
            else:
                paths = [p for p in (normalize_reserved_path(x) for x in req.paths) if p]
                if not paths:
                    raise HTTPException(400, "Name the paths to release, or release all")
                cur = conn.execute(
                    f"DELETE FROM reservations WHERE room = ? AND holder = ? AND path IN"
                    f" ({','.join('?' * len(paths))})", (who.room, who.display_name, *paths))
            conn.commit()
        return {"ok": True, "released": cur.rowcount}

    @app.get("/v1/reservations")
    def reservations(who: Caller = Depends(caller)) -> dict:
        with lock:
            rows = _active_reservations(who.room, time.time())
        return {"reservations": [{"path": r["path"], "holder": r["holder"], "reason": r["reason"],
                                  "exclusive": bool(r["exclusive"]), "expires_at": r["expires_at"]}
                                 for r in rows[:1000]], "total": len(rows)}

    # --- join links -------------------------------------------------------------
    #
    # bridge.py host makes a short code (XXXX-XXXX, 40 random bits) and prints a
    # link, https://<relay>/j/<code>, that anyone can open or paste to their
    # agent. The code stands in for the room token: whoever has the link can
    # join, exactly as with the long invite. Wrong guesses share one small
    # budget per minute for the whole relay, so 40 bits cannot be guessed.
    codes = {_normalize_code(c): v for c, v in (invite_codes or {}).items() if _normalize_code(c)}
    code_misses = _Window(INVITE_GUESSES_PER_MINUTE, RATE_LIMIT_WINDOW_SECONDS)

    def _resolve_code(raw: str) -> tuple[str, str]:
        code = _normalize_code(raw)
        now = time.time()
        with lock:
            match = None
            for known, value in codes.items():
                if code and hmac.compare_digest(known, code):
                    match = value
            if match is not None:
                return match
            # Only wrong codes use up the budget, so guessing cannot lock out
            # the people who were really sent the link.
            if not code_misses.allow("*", now):
                raise HTTPException(429, "Too many wrong codes - try again in a minute")
        raise HTTPException(404, "This join link is not valid on this bridge (it may be from an earlier run)")

    @app.post("/v1/invite")
    def resolve_invite(req: InviteRequest) -> dict:
        room, token = _resolve_code(req.code)
        return {"room": room, "token": token, "version": __version__}

    @app.get("/j/{code}")
    def join_page(code: str, request: Request) -> Response:
        """What a person or an agent sees when they open a join link."""
        _resolve_code(code)  # a wrong code gets a 404, not instructions
        # Behind cloudflared the relay itself is spoken to over plain HTTP; the
        # link people use is the tunnel's https one.
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
        scheme = scheme if scheme in ("http", "https") else request.url.scheme
        host = request.headers.get("host") or ""
        if not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}(?::[0-9]{1,5})?|\[[0-9A-Fa-f:]+\](?::[0-9]{1,5})?", host):
            server = request.scope.get("server") or ("127.0.0.1", 80)
            host = f"{server[0]}:{server[1]}"  # the socket's own address, never header text
        link = f"{scheme}://{host}/j/{_normalize_code(code)}"
        text = JOIN_PAGE.format(link=link)
        return Response(content=text, media_type="text/plain; charset=utf-8",
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                 "X-Robots-Tag": "noindex"})

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
