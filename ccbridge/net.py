"""HTTP clients for talking to a relay."""

from __future__ import annotations

import httpx

# A host's own relay is on this machine. httpx otherwise sends even these
# requests through a proxy from HTTP_PROXY - or, on Windows, from the system
# proxy settings - and a proxy cannot reach your loopback address.
_NO_PROXY_FOR_LOOPBACK = {
    "all://127.0.0.1": None,
    "all://localhost": None,
    "all://[::1]": None,
}


def http_client(**kwargs) -> httpx.Client:
    """An httpx.Client that never sends loopback requests through a proxy."""
    return httpx.Client(mounts=_NO_PROXY_FOR_LOOPBACK, **kwargs)
