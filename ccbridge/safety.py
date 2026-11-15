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

import functools
import re
import unicodedata

# Per-message cap. Big enough for a detailed handoff note, small enough that a
# peer cannot flood the receiving agent's context window.
MAX_MESSAGE_BYTES = 8192
MAX_NAME_LEN = 64

# Display names are the only identity a peer sees, so keep them boring: no
# markup, no newlines, nothing that could pass for framing. Single spaces only
# between words, so "alice" and "alice " cannot both exist. \Z, not $: "$" also
# matches before a trailing newline.
NAME_RE = re.compile(rf"^(?=.{{1,{MAX_NAME_LEN}}}\Z)[A-Za-z0-9_.\-]+(?: [A-Za-z0-9_.\-]+)*\Z")
ROOM_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,64}\Z")

FLAG_INVISIBLE = "invisible-characters-removed"
FLAG_CONTROL = "control-characters-removed"
FLAG_FRAMING = "harness-framing-neutralized"
KNOWN_FLAGS = frozenset({FLAG_INVISIBLE, FLAG_CONTROL, FLAG_FRAMING})

# Framing a peer could otherwise use to impersonate the harness or the user. This
# is a second line of defence: the delivery banner's random tag is what actually
# stops a peer from "closing" the untrusted block early.
#
# Every pattern must run in linear time: the relay runs them on text from anyone
# holding the room token, so a pattern that backtracks is a denial of service.
#
# Harness-style tags, closed or not. The tag name must not run on into a longer
# identifier ("<system_error>" is a C++ header). _tag_spans() decides where each
# one ends.
_TAG_OPEN = re.compile(
    r"<(?:\s*/)?\s*(?:system(?:-[\w-]*)?|human|assistant|cross-session-message|task-notification"
    r"|ci-monitor-event|function_(?:calls|results)|antml:[^\s>]*|local-command-[a-z]+"
    r"|command-(?:name|message|args)|user-prompt-submit-hook|bash-(?:input|stdout|stderr)"
    r"|tool_(?:use|result))(?![\w-])",
    re.I,
)

_SPOOF_PATTERNS = (
    # Chat-transcript turn markers at the start of a line. [^\S\n] is any space
    # character except a newline, including the exotic ones NFKC leaves alone.
    re.compile(r"^[^\S\n]*(?:Human|Assistant)[^\S\n]*:", re.I | re.M),
    re.compile(r"\[SYSTEM NOTIFICATION[^\]\n]*(?:\]|$)", re.I | re.M),
    re.compile(r"\[Artifact comment sent to Claude\]", re.I),
    # Anything shaped like our own delivery banner, however it is spaced. The
    # lookbehind makes each run of "=" start matching only once; without it, a
    # long run of "=" backtracks quadratically.
    re.compile(r"(?<!=)=+[^\S\n]*(?:END[^\S\n]+)?PEER[^\S\n]+MESSAGE[^\n]*", re.I),
)

# Brackets, equals signs and colons become look-alikes: the text stays readable to
# a human but can no longer be parsed as a tag, a turn marker, or a forged copy of
# the delivery banner. Applied only inside a matched span, so ordinary prose and
# code elsewhere in the message keep their real characters. None of these
# look-alikes change under NFKC, so sanitizing twice is stable.
_NEUTRALIZE = str.maketrans(
    {"<": "‹", ">": "›", "[": "⟦", "]": "⟧", "=": "═", ":": "꞉"}
)
_NEUTRALIZED_CHARS = frozenset("<>[]=:")

# Default-ignorable code points render as nothing, so a reader does not see them,
# but they would split a keyword for a regex ("<sys͏tem-reminder>"). Some
# are ordinary writing (variation selectors in emoji), so they are dropped only
# from the copy the patterns run on, never from what is delivered.
_IGNORABLE_RANGES = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160),
    (0x17B4, 0x17B5), (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E),
    (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
)

# Invisible or text-reordering characters: format controls (zero-width joiners,
# bidi overrides) and unpaired surrogates.
_INVISIBLE_CATEGORIES = {"Cf", "Cs"}

# Zero-width (non-)joiners are part of ordinary writing: emoji sequences, Persian,
# the Indic scripts. They are kept between two non-ASCII characters, where they
# cannot split an ASCII keyword such as "system" into something the patterns miss.
_JOINERS = {"\u200c", "\u200d"}


def _fold(ch: str) -> str:
    return unicodedata.normalize("NFKC", ch)


def _ignorable(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _IGNORABLE_RANGES)


@functools.lru_cache(maxsize=4096)
def _detect_fold(ch: str) -> str:
    """What the patterns see for ``ch``: its compatibility form, minus accents and
    anything invisible. "＜", "ｓ" and "ý" read as "<", "s" and "y"; U+034F reads
    as nothing."""
    if _ignorable(ch):
        return ""
    return "".join(c for c in unicodedata.normalize("NFKD", ch)
                   if unicodedata.category(c) not in ("Mn", "Me") and not _ignorable(c))


def _neutralize(ch: str) -> str:
    """How ``ch`` is written inside a span of forged framing."""
    seen = _detect_fold(ch)
    if not seen:
        return ""  # invisible padding has no business inside forged framing
    if _NEUTRALIZED_CHARS.intersection(seen):
        return seen.translate(_NEUTRALIZE)  # "＜" becomes "‹", not a fullwidth "<"
    return ch


def _joins_writing(text: str, i: int) -> bool:
    if i == 0 or i == len(text) - 1:
        return False
    for ch in (text[i - 1], text[i + 1]):
        if ch.isspace() or unicodedata.category(ch) in _INVISIBLE_CATEGORIES | {"Cc"}:
            return False
        if _fold(ch).isascii():
            return False
    return True


def _strip_invisible(text: str) -> str:
    return "".join(
        ch for i, ch in enumerate(text)
        if unicodedata.category(ch) not in _INVISIBLE_CATEGORIES
        or (ch in _JOINERS and _joins_writing(text, i))
    )


def _tag_spans(folded: str) -> list[tuple[int, int]]:
    """Spans of forged harness tags in ``folded``, in one linear pass.

    A tag runs to its ">" or, if unclosed, to the end of its line, so a dangling
    "<system-reminder" cannot slip through. One shape is exempt because it is
    everyday code and never a tag: "< system.attr" with no ">" after it on the
    line, as in "if free_mb < system.limit:".
    """
    n = len(folded)
    # stop[i]: where the first ">" or newline at or after i is (n if none). Built
    # once, so finding where each tag ends costs nothing however many there are.
    stop = [n] * (n + 1)
    for i in range(n - 1, -1, -1):
        stop[i] = i if folded[i] in ">\n" else stop[i + 1]
    spans = []
    for m in _TAG_OPEN.finditer(folded):
        end = stop[m.end()]
        closed = end < n and folded[end] == ">"
        after = folded[m.end():m.end() + 2]
        if not closed and len(after) == 2 and after[0] == "." and (after[1].isalnum() or after[1] == "_"):
            continue
        spans.append((m.start(), end + 1 if closed else end))
    return spans


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def sanitize(text: str) -> tuple[str, list[str]]:
    """Return ``(clean_text, flags)`` for a message body.

    ``flags`` names each defence that actually fired, so the receiving agent can
    be told that the sender tried something. An empty list means nothing
    suspicious was found.

    Detection runs on a compatibility-folded copy (fullwidth brackets and
    non-breaking spaces become their plain forms), so a look-alike cannot hide
    framing from the patterns. Only the matched spans are rewritten; everything
    else is delivered as the sender wrote it, so "10\u207b\u00b3" stays "10\u207b\u00b3".
    """
    flags: list[str] = []

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u2028", "\n").replace("\u2029", "\n")

    stripped = _strip_invisible(text)
    if stripped != text:
        flags.append(FLAG_INVISIBLE)
    text = stripped

    # Drop control characters (C0, DEL, C1) other than newline and tab.
    cleaned = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) != "Cc")
    if cleaned != text:
        flags.append(FLAG_CONTROL)
    text = cleaned

    # Fold one character at a time, remembering where each folded character came
    # from, so a match in the folded copy maps back onto the original text.
    pieces = [_detect_fold(ch) for ch in text]
    folded = "".join(pieces)
    origin = [i for i, piece in enumerate(pieces) for _ in piece]

    spans = _tag_spans(folded) + [m.span() for pattern in _SPOOF_PATTERNS
                                  for m in pattern.finditer(folded) if m.end() > m.start()]
    spans = [(s, e) for s, e in spans if e > s]
    if not spans:
        return text, flags

    flags.append(FLAG_FRAMING)
    out: list[str] = []
    done = 0
    for start, end in _merge([(origin[s], origin[e - 1] + 1) for s, e in spans]):
        out.append(text[done:start])
        out.append("".join(_neutralize(ch) for ch in text[start:end]))
        done = end
    out.append(text[done:])
    return "".join(out), flags


# Outbound: credentials an agent should never paste into a message to another
# person, however politely the peer asked. Deliberately specific (known token
# shapes, PEM blocks, literal assignments) so ordinary code and prose pass: a
# refused message costs the user a round trip, so a false alarm is not free.
_PLACEHOLDER = re.compile(r"your|change.?me|example|placeholder|redacted|dummy|xxxx|[<>*]", re.I)
_PLACEHOLDER_PASSWORDS = {"password", "passwd", "pass", "pwd", "secret"}


def _real_value(match: re.Match) -> bool:
    """A credential-like value: letters and digits, and not a placeholder."""
    value = match.group("value")
    return (any(c.isdigit() for c in value) and any(c.isalpha() for c in value)
            and not _PLACEHOLDER.search(value))


def _real_url_password(match: re.Match) -> bool:
    user, password = match.group("user"), match.group("password")
    if _PLACEHOLDER.search(user + ":" + password):
        return False
    if password.lower() in _PLACEHOLDER_PASSWORDS or (password.isalpha() and password.isupper()):
        return False  # postgresql://USER:PASSWORD@HOST is documentation
    return True


_SECRET_PATTERNS = (
    ("a private key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"), None),
    ("an AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), None),
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"), None),
    ("a Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), None),
    ("a Stripe secret key", re.compile(r"\b[rs]k_live_[A-Za-z0-9]{16,}"), None),
    # Real keys always contain digits; "sk-refactor-the-relay-..." is a branch name.
    ("an AI provider API key",
     re.compile(r"\bsk-(?:ant-|proj-)?(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{32,}"), None),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), None),
    ("a JSON Web Token",
     re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), None),
    # The lookbehind starts a match only at the beginning of a scheme, which keeps
    # this linear on text like "a.a.a.a...".
    ("a URL with a password in it",
     re.compile(r"(?<![a-z0-9+.-])[a-z][a-z0-9+.-]*://(?P<user>[^/\s:@?#]+):(?P<password>[^/\s@?#]+)@",
                re.I),
     _real_url_password),
    # SHOUTY_SNAKE names only, with the keyword as a whole segment: GITHUB_TOKEN
    # and DB_PASSWORD, not max_new_tokens, tokenizer or passwordInput.
    ("a password or secret assignment", re.compile(
        r"^[ \t]*(?:export[ \t]+)?(?:[A-Z0-9]+_)*(?:SECRET|PASSWORD|PASSWD|TOKEN|API_?KEY|PRIVATE_KEY"
        r"|ACCESS_KEY)(?:_[A-Z0-9]+)*[ \t]*[:=][ \t]*['\"]?(?P<value>[A-Za-z0-9/+_.~\-]{12,})['\"]?[ \t]*$",
        re.M), _real_value),
)


def find_secrets(text: str) -> list[str]:
    """Return a description of each kind of credential found in ``text``."""
    return [label for label, pattern, check in _SECRET_PATTERNS
            if any(check is None or check(m) for m in pattern.finditer(text))]


def too_large(text: str) -> bool:
    # surrogatepass: a lone surrogate must count toward the size, not crash it.
    return len(text.encode("utf-8", "surrogatepass")) > MAX_MESSAGE_BYTES


def safe_name(name: object) -> str | None:
    """Return ``name`` if it is a well-formed display name, else ``None``."""
    return name if isinstance(name, str) and NAME_RE.match(name) else None
