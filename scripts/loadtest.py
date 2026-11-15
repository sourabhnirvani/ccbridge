"""Load-test a ccbridge relay with many simulated agents.

    python scripts/loadtest.py --agents 1000 --seconds 60

Starts a relay in its own process (a fresh database in a temp folder), then runs
N agents the way the MCP server does: each joins, keeps a waiting poll open
(get_messages with wait), heartbeats every 30 s, sends a direct message to a
random peer every ~30 s, and one "lead" broadcasts to the room every few
seconds. Reports request latencies, delivery lag and errors.

The generator is one Python process: with many agents its own event loop is
the bottleneck, so its latencies are an upper bound. To see the relay's own
latency under load, probe it from a separate process while this runs.

Or point it at a running relay with --url (it needs an open relay, or pass the
room's token with --token and --room).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import secrets
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pct(values, p):
    if not values:
        return float("nan")
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p / 100))]


class Stats:
    def __init__(self):
        self.lat = defaultdict(list)
        self.errors = defaultdict(int)
        self.lag = []
        self.delivered = 0
        self.sent = 0

    def report(self, agents, seconds):
        print(f"\n=== {agents} agents, {seconds:.0f} s ===")
        for name, vals in sorted(self.lat.items()):
            print(f"{name:10s} n={len(vals):7d}  p50={pct(vals, 50) * 1000:7.1f} ms  "
                  f"p95={pct(vals, 95) * 1000:7.1f} ms  p99={pct(vals, 99) * 1000:7.1f} ms")
        print(f"messages sent: {self.sent}, deliveries: {self.delivered}")
        if self.lag:
            print(f"delivery lag   p50={pct(self.lag, 50) * 1000:7.1f} ms  p95={pct(self.lag, 95) * 1000:7.1f} ms"
                  f"  max={max(self.lag) * 1000:7.1f} ms")
        print("errors:", dict(self.errors) or "none")


async def timed(stats, name, coro):
    t = time.perf_counter()
    try:
        resp = await coro
    except Exception as exc:  # noqa: BLE001
        stats.errors[f"{name}:{type(exc).__name__}"] += 1
        return None
    stats.lat[name].append(time.perf_counter() - t)
    if resp.status_code != 200:
        stats.errors[f"{name}:{resp.status_code}"] += 1
        return None
    return resp


async def agent(i, args, http, stats, names, stop, ready):
    name = f"agent-{i:05d}"
    body = {"room": args.room, "display_name": name, "identity_key": f"load-key-{name}-xxxxxxxx",
            "identity_scheme": 2, "client": random.choice(["claude-code", "codex", "cursor", "grok"])}
    auth = {"Authorization": f"Bearer {args.token}"}
    for attempt in range(20):
        resp = await timed(stats, "join", http.post("/v1/join", json=body, headers=auth))
        if resp is not None:
            break
        await asyncio.sleep(1 + random.random() * 3)
    else:
        return
    data = resp.json()
    h = {**auth, "X-CCBridge-Room": args.room, "X-CCBridge-Session": data["session_id"]}
    cursor = data["cursor"]
    ready.append(name)

    async def beat():
        while not stop.is_set():
            await asyncio.sleep(25 + random.random() * 10)
            await timed(stats, "heartbeat", http.post("/v1/heartbeat", params={"ack": cursor}, headers=h))

    async def talk():
        await asyncio.sleep(random.random() * args.dm_every)
        while not stop.is_set():
            to = random.choice(names)
            if to != name:
                stats.sent += 1
                await timed(stats, "send", http.post(
                    "/v1/send", json={"text": f"t={time.time()}", "to": to, "id": secrets.token_urlsafe(12)},
                    headers=h))
            await asyncio.sleep(args.dm_every * (0.5 + random.random()))

    tasks = [asyncio.create_task(beat()), asyncio.create_task(talk())]
    while not stop.is_set():
        resp = await timed(stats, "poll", http.get(
            "/v1/poll", params={"since": cursor, "limit": 100, "wait": 20}, headers=h))
        if resp is None:
            await asyncio.sleep(1)
            continue
        got = resp.json()
        now = time.time()
        for m in got["messages"]:
            stats.delivered += 1
            if m["text"].startswith("t="):
                stats.lag.append(now - float(m["text"][2:]))
        cursor = got["cursor"]
    for t in tasks:
        t.cancel()


async def lead(args, http, stats, stop):
    body = {"room": args.room, "display_name": "lead", "identity_key": "load-key-lead-xxxxxxxxxx",
            "identity_scheme": 2}
    auth = {"Authorization": f"Bearer {args.token}"}
    data = (await http.post("/v1/join", json=body, headers=auth)).json()
    h = {**auth, "X-CCBridge-Room": args.room, "X-CCBridge-Session": data["session_id"]}
    while not stop.is_set():
        await asyncio.sleep(args.broadcast_every)
        stats.sent += 1
        await timed(stats, "broadcast", http.post("/v1/send", json={"text": f"t={time.time()}"}, headers=h))


async def run(args):
    stats = Stats()
    stop = asyncio.Event()
    limits = httpx.Limits(max_connections=args.agents * 3 + 50, max_keepalive_connections=args.agents * 3)
    async with httpx.AsyncClient(base_url=args.url, timeout=60, limits=limits) as http:
        names = [f"agent-{i:05d}" for i in range(args.agents)]
        ready: list[str] = []
        t0 = time.perf_counter()
        workers = [asyncio.create_task(agent(i, args, http, stats, names, stop, ready))
                   for i in range(args.agents)]
        while len(ready) < args.agents and time.perf_counter() - t0 < 120:
            await asyncio.sleep(0.5)
        print(f"{len(ready)} agents joined in {time.perf_counter() - t0:.1f} s")
        boss = asyncio.create_task(lead(args, http, stats, stop))
        await asyncio.sleep(args.seconds)
        stop.set()
        await asyncio.sleep(0)
        for w in workers + [boss]:
            w.cancel()
        await asyncio.gather(*workers, boss, return_exceptions=True)
    stats.report(args.agents, args.seconds)
    return stats


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--agents", type=int, default=1000)
    p.add_argument("--seconds", type=float, default=60)
    p.add_argument("--dm-every", type=float, default=30, help="seconds between one agent's direct messages")
    p.add_argument("--broadcast-every", type=float, default=5)
    p.add_argument("--url")
    p.add_argument("--room", default="loadtest")
    p.add_argument("--token", default="load-token")
    args = p.parse_args()

    proc = None
    if not args.url:
        port = free_port()
        db = Path(tempfile.mkdtemp()) / "load.db"
        env = {**os.environ, "CCBRIDGE_DB": str(db), "CCBRIDGE_MAX_MEMBERS": str(args.agents + 10),
               "PYTHONPATH": str(HERE)}
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "ccbridge.relay:app_from_env", "--factory",
                                 "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
                                 "--backlog", "4096", "--limit-concurrency", "100000"], env=env, cwd=HERE)
        args.url = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if httpx.get(args.url + "/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
    try:
        stats = asyncio.run(run(args))
    finally:
        if proc:
            proc.terminate()
            proc.wait(10)
    bad = sum(stats.errors.values())
    print(json.dumps({"agents": args.agents, "errors": bad}))
    sys.exit(1 if bad > len(stats.lat.get("poll", [])) * 0.01 else 0)


if __name__ == "__main__":
    main()
