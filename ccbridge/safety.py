"""Safety helpers shared by the relay and the MCP server.

This bridge carries text between two *different people's* agents. Every inbound
message is therefore untrusted input, and two things have to hold:

1. A peer must not be able to forge harness framing - a fake ``<system-reminder>``,
   task notification, or tool result - to smuggle instructions past the receiving
   agent as if they came from its own system. ``sanitize()`` handles this.
2. The receiving agent must always see the message's origin and be told that a
   peer cannot authorize anything. ``mcp_server.wrap_peer_message()`` handles that,
   inside a banner carrying a random tag the peer cannot know in advance.

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

# Display names are the only identity a peer sees, so keep them boring: no
# markup, no newlines, nothing that could pass for framing.
NAME_RE = re.compile(rf"^[A-Za-z0-9 _.\-]{{1,{MAX_NAME_LEN}}}$")
ROOM_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,64}$")

FLAG_INVISIBLE = "invisible-characters-removed"
FLAG_CONTROL = "control-characters-removed"
FLAG_FRAMING = "harness-framing-neutralized"
KNOWN_FLAGS = frozenset({FLAG_INVISIBLE, FLAG_CONTROL, FLAG_FRAMING})

# Framing a peer could otherwise use to impersonate the harness or the user. This
# is a second line of defence: the delivery banner's random tag is what actually
# stops a peer from "closing" the untrusted block early.
_SPOOF_PATTERNS = (
    # Harness-style tags, closed or not. An unclosed tag runs to the end of the
    # text, so a dangling "<system-reminder" cannot slip through either.
    re.compile(
        r"</?\s*(?:system(?:-[\w-]+)?|human|assistant|cross-session-message|task-notification"
        r"|ci-monitor-event|function_(?:calls|results)|antml:[^\s>]*|local-command-[a-z]+"
        r"|command-(?:name|message|args)|user-prompt-submit-hook|bash-(?:input|stdout|stderr)"
        r"|tool_(?:use|result))\b[^>]*(?:>|$)",
        re.I,
    ),
    # Chat-transcript turn markers at the start of a line.
    re.compile(r"^[ \t]*(?:Human|Assistant)[ \t]*:", re.I | re.M),
    re.compile(r"\[SYSTEM NOTIFICATION[^\]]*\]", re.I),
    re.compile(r"\[Artifact comment sent to Claude\]", re.I),
    # Anything shaped like our own delivery banner, however it is spaced.
    re.compile(r"=+[ \t]*(?:END[ \t]+)?PEER[ \t]+MESSAGE[^\n]*", re.I),
)

# Brackets, equals signs and colons become look-alikes: the text stays readable to
# a human but can no longer be parsed as a tag, a turn marker, or a forged copy of
# the delivery banner. Applied only inside a matched span, so ordinary prose and
# code elsewhere in the message keep their real characters. None of these
# look-alikes change under NFKC, so sanitizing twice is stable.
_NEUTRALIZE = str.maketrans(
    {"<": "‹", ">": "›", "[": "⟦", "]": "⟧", "=": "═", ":": "꞉"}
)

# Invisible or text-reordering characters: format controls (zero-width joiners,
# bidi overrides) and unpaired surrogates.
_INVISIBLE_CATEGORIES = {"Cf", "Cs"}


def sanitize(text: str) -> tuple[str, list[str]]:
    """Return ``(clean_text, flags)`` for a message body.

    ``flags`` names each defence that actually fired, so the receiving agent can
    be told that the sender tried something. An empty list means nothing
    suspicious was found.
    """
    flags: list[str] = []

    # Fold compatibility look-alikes (fullwidth brackets, non-breaking spaces)
    # into their plain forms first, so the patterns below see what a reader sees.
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u2028", "\n").replace("\u2029", "\n")

    stripped = "".join(ch for ch in text if unicodedata.category(ch) not in _INVISIBLE_CATEGORIES)
    if stripped != text:
        flags.append(FLAG_INVISIBLE)
    text = stripped

    # Drop control characters (C0, DEL, C1) other than newline and tab.
    cleaned = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) != "Cc")
    if cleaned != text:
        flags.append(FLAG_CONTROL)
    text = cleaned

    for pattern in _SPOOF_PATTERNS:
        text, count = pattern.subn(lambda m: m.group(0).translate(_NEUTRALIZE), text)
        if count and FLAG_FRAMING not in flags:
            flags.append(FLAG_FRAMING)

    return text, flags


def too_large(text: str) -> bool:
    # surrogatepass: a lone surrogate must count toward the size, not crash it.
    return len(text.encode("utf-8", "surrogatepass")) > MAX_MESSAGE_BYTES


def safe_name(name: object) -> str | None:
    """Return ``name`` if it is a well-formed display name, else ``None``."""
    return name if isinstance(name, str) and NAME_RE.match(name) else None
