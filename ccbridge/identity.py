"""How a client proves to a relay that it owns its display name.

Shared by the MCP server and bridge.py, which claims your name in the room as
soon as you run host or join.
"""

from __future__ import annotations

import hashlib
import hmac

# The proof format this client sends. A relay turns away anything older.
IDENTITY_SCHEME = 2

# Must match ccbridge.relay.LEGACY_CLAIM_HEADER. A relay sends it with a 409 when
# the name is a claim carried over from ccbridge 0.2.
LEGACY_CLAIM_HEADER = "X-CCBridge-Legacy-Claim"


def identity_proof(key: str, room: str) -> str:
    """What this person shows a relay to prove they own their name in ``room``.

    Never the key itself: a relay operator (or anyone who can read its traffic,
    such as the tunnel provider) would then be able to use your key to take
    your name on every other relay you use.
    """
    mac = hmac.new(key.encode("utf-8", "surrogatepass"), digestmod=hashlib.sha256)
    mac.update(b"ccbridge-identity-v1\0" + room.encode("utf-8"))
    return mac.hexdigest()


def legacy_key_hash(key: str) -> str:
    """The hash a 0.2 relay stored for ``key``: proves you held a 0.2 claim."""
    return hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()


def join_body(room: str, name: str, key: str, *, register_only: bool = False,
              client: str | None = None) -> dict:
    """The body of a POST /v1/join.

    ``client`` says what kind of agent this is (claude-code, codex, cursor, ...);
    peers see it next to the name. A relay older than 0.4 ignores it.
    """
    body = {"room": room, "display_name": name, "identity_key": identity_proof(key, room),
            "identity_scheme": IDENTITY_SCHEME}
    if register_only:
        body["register_only"] = True
    if client:
        body["client"] = client
    return body
