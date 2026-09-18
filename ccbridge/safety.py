"""Safety helpers shared by the relay and the MCP server.

This bridge carries text between two *different people's* agents. Every inbound
message is therefore untrusted input, and two things have to hold:

1. A peer must not be able to forge harness framing - a fake ``<system-reminder>``,
   task notification, or tool result - to smuggle instructions past the receiving
   agent as if they came from its own system. ``sanitize()`` handles this.
2. The receiving agent must always see the message's origin and be told that a
   peer cannot authorize anything. ``mcp_server.wrap_peer_message()`` handles that.

Sanitizing happens on the relay (so a hostile client cannot skip it) and again on
delivery in the MCP server (so a hostile *relay* cannot skip it either).
"""

from __future__ import annotations

import re
import unicodedata

# Per-message cap. Big enough for a detailed handoff note, small enough that a
# peer cannot flood the receiving agent's context window.
MAX_MESSAGE_BYTES = 8192
MAX_NAME_LEN = 64

# Framing a peer could otherwise use to impersonate the harness or the user.
_SPOOF_PATTERNS = (
    re.compile(r"</?\s*system-reminder\b[^>]*>", re.I),
    re.compile(r"</?\s*cross-session-message\b[^>]*>", re.I),
    re.compile(r"</?\s*task-notification\b[^>]*>", re.I),
    re.compile(r"</?\s*ci-monitor-event\b[^>]*>", re.I),
    re.compile(r"</?\s*function_(?:calls|results)\b[^>]*>", re.I),
    re.compile(r"</?\s*antml:[^>]*>", re.I),
    re.compile(r"</?\s*local-command-[a-z]+\b[^>]*>", re.I),
    re.compile(r"</?\s*command-(?:name|message|args)\b[^>]*>", re.I),
    re.compile(r"\[SYSTEM NOTIFICATION[^\]]*\]", re.I),
    re.compile(r"\[Artifact comment sent to Claude\]", re.I),
    # A peer must not be able to fake the start/end of our own delivery banner
    # and thereby appear to "escape" the untrusted block.
    re.compile(r"=+\s*PEER MESSAGE[^=\n]*=+", re.I),
    re.compile(r"=+\s*END PEER MESSAGE\s*=+", re.I),
)

# Brackets and equals signs become look-alikes: the text stays readable to a
# human but can no longer be parsed as a tag, a harness marker, or a forged copy
# of our own delivery banner. Applied only inside a matched span, so ordinary
# prose and code elsewhere in the message keep their real characters.
_NEUTRALIZE = str.maketrans({"<": "‹", ">": "›", "[": "⟦", "]": "⟧", "=": "═"})


def sanitize(text: str) -> tuple[str, list[str]]:
    """Return ``(clean_text, flags)`` for a message body.

    ``flags`` names each defence that actually fired, so the receiving agent can
    be told that the sender tried something. An empty list means the message was
    already clean.
    """
    flags: list[str] = []

    # Strip Unicode format characters (zero-width joiners, bidi overrides). These
    # render as nothing but can hide or visually reorder text.
    stripped = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    if stripped != text:
        flags.append("invisible-characters-removed")
    text = stripped

    # Drop C0 control characters other than newline and tab.
    cleaned = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    if cleaned != text:
        flags.append("control-characters-removed")
    text = cleaned

    for pattern in _SPOOF_PATTERNS:
        text, count = pattern.subn(lambda m: m.group(0).translate(_NEUTRALIZE), text)
        if count and "harness-framing-neutralized" not in flags:
            flags.append("harness-framing-neutralized")

    return text, flags


def too_large(text: str) -> bool:
    return len(text.encode("utf-8")) > MAX_MESSAGE_BYTES
