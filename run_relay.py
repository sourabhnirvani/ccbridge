"""Start the relay.

    python run_relay.py                  # 127.0.0.1:8787, local only
    CCBRIDGE_HOST=0.0.0.0 python run_relay.py

Bind to 0.0.0.0 only behind HTTPS (a tunnel or a reverse proxy). The room token
travels in an Authorization header, so plain HTTP over the open internet would
hand it to anyone on the path.
"""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "ccbridge.relay:app",
        host=os.getenv("CCBRIDGE_HOST", "127.0.0.1"),
        port=int(os.getenv("CCBRIDGE_PORT", "8787")),
    )
