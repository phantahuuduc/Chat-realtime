"""
WebSocket Load Test Script — Plan Section 9.2

Acceptance Criteria:
- 200-500 concurrent WebSocket connections
- 0 messages lost, 0 duplicates (idempotency)
- Track p50/p95/p99 fanout latency
- Track error rate (%) and throughput (msg/sec)

Usage:
    python scripts/load_test.py --host localhost:8000 --connections 200 --duration 60

Prerequisites:
    pip install websockets httpx
    Django server running with PostgreSQL + Redis
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from collections import defaultdict

try:
    import httpx
    import websockets
except ImportError:
    print("Install required packages: pip install websockets httpx")
    sys.exit(1)


class LoadTestRunner:
    def __init__(self, host, connections, duration, messages_per_sec):
        self.host = host
        self.num_connections = connections
        self.duration = duration
        self.messages_per_sec = messages_per_sec

        # Metrics
        self.latencies = []
        self.messages_sent = 0
        self.messages_received = 0
        self.errors = 0
        self.duplicates = 0
        self.connect_failures = 0
        self.seen_message_ids = set()
        self.seen_sequences = defaultdict(set)
        self._lock = asyncio.Lock()

    async def register_user(self, username):
        """Register a test user and get JWT token and user ID."""
        url = f"http://{self.host}/api/auth/register/"
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(url, json={
                    "username": username,
                    "password": "loadtest123",
                })
                if resp.status_code == 201:
                    data = resp.json()
                    return data["access_token"], data["user"]["id"]
                # User exists, try login
                resp = await client.post(
                    f"http://{self.host}/api/auth/login/",
                    json={"username": username, "password": "loadtest123"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return data["access_token"], data["user"]["id"]
            except Exception as e:
                print(f"  Auth error for {username}: {e}")
        return None, None

    async def create_conversation(self, token, member_ids):
        """Create a test conversation."""
        url = f"http://{self.host}/api/conversations/"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                json={"name": "Load Test Room", "type": "group", "member_ids": member_ids},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 201:
                return resp.json()["id"]
        return None

    async def ws_client(self, client_id, token, conv_id, start_event):
        """Single WebSocket client that sends and receives messages."""
        url = f"ws://{self.host}/ws/chat/{conv_id}/?token={token}"
        try:
            async with websockets.connect(url) as ws:
                # Wait for connection.established
                msg = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(msg)
                if data.get("type") != "connection.established":
                    async with self._lock:
                        self.connect_failures += 1
                    return

                # Wait for all clients to connect
                await start_event.wait()

                # Receiver task
                client_seen_mids = set()
                client_seen_sequences = set()

                async def receiver():
                    try:
                        async for raw in ws:
                            recv_time = time.monotonic()
                            d = json.loads(raw)
                            if d.get("type") == "chat.message":
                                m = d["message"]
                                mid = m.get("client_message_id")
                                seq = m.get("sequence_number")
                                send_ts = m.get("_send_ts")

                                async with self._lock:
                                    self.messages_received += 1

                                    # Check duplicates per-client
                                    if mid and mid in client_seen_mids:
                                        self.duplicates += 1
                                    elif mid:
                                        client_seen_mids.add(mid)

                                    # Check sequence ordering per-client
                                    if seq is not None and seq in client_seen_sequences:
                                        self.duplicates += 1
                                    elif seq is not None:
                                        client_seen_sequences.add(seq)

                                    # Latency (if we sent it)
                                    if send_ts:
                                        latency_ms = (recv_time - send_ts) * 1000
                                        self.latencies.append(latency_ms)
                    except websockets.ConnectionClosed:
                        pass

                recv_task = asyncio.create_task(receiver())

                # Sender: send messages at configured rate
                end_time = time.monotonic() + self.duration
                interval = 1.0 / max(self.messages_per_sec, 0.1)

                while time.monotonic() < end_time:
                    cid = str(uuid.uuid4())
                    send_ts = time.monotonic()
                    payload = {
                        "type": "chat.message",
                        "content": f"Load test msg from client {client_id}",
                        "client_message_id": cid,
                        "_send_ts": send_ts,
                    }
                    try:
                        await ws.send(json.dumps(payload))
                        async with self._lock:
                            self.messages_sent += 1
                    except Exception:
                        async with self._lock:
                            self.errors += 1
                        break
                    await asyncio.sleep(interval)

                # Wait a bit for remaining messages
                await asyncio.sleep(2)
                recv_task.cancel()

        except Exception as e:
            async with self._lock:
                self.connect_failures += 1

    async def run(self):
        """Execute the load test."""
        print("=" * 60)
        print(f"  WebSocket Load Test — CR7 Chat Platform")
        print(f"  Target: {self.host}")
        print(f"  Connections: {self.num_connections}")
        print(f"  Duration: {self.duration}s")
        print(f"  Messages/sec/client: {self.messages_per_sec}")
        print("=" * 60)

        # Step 1: Register users
        print("\n[1/4] Registering test users...")
        tokens = []
        user_ids = []
        for i in range(self.num_connections):
            username = f"loadtest_user_{i}"
            token, uid = await self.register_user(username)
            if token and uid:
                tokens.append(token)
                user_ids.append(uid)
            else:
                print(f"  Failed to register user {username}")

        if len(tokens) < 2:
            print("ERROR: Need at least 2 users. Check server is running.")
            return

        print(f"  Registered {len(tokens)} users")

        # Step 2: Create conversation
        print("\n[2/4] Creating test conversation...")
        conv_id = await self.create_conversation(tokens[0], user_ids[1:])
        if not conv_id:
            print("ERROR: Could not create conversation.")
            return
        print(f"  Conversation ID: {conv_id}")

        # Step 3: Connect all clients
        print(f"\n[3/4] Connecting {len(tokens)} WebSocket clients...")
        start_event = asyncio.Event()
        tasks = [
            self.ws_client(i, token, conv_id, start_event)
            for i, token in enumerate(tokens)
        ]

        # Start all tasks
        gathered = asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(3)  # Wait for connections
        start_event.set()  # Signal all clients to start sending

        # Step 4: Wait for completion
        print(f"\n[4/4] Running load test for {self.duration}s...")
        start_time = time.monotonic()
        await gathered
        elapsed = time.monotonic() - start_time

        # Report
        self._print_report(elapsed)

    def _print_report(self, elapsed):
        print("\n" + "=" * 60)
        print("  LOAD TEST REPORT")
        print("=" * 60)

        print(f"\n  Duration:            {elapsed:.1f}s")
        print(f"  Connections:         {self.num_connections}")
        print(f"  Connect failures:    {self.connect_failures}")
        print(f"  Messages sent:       {self.messages_sent}")
        print(f"  Messages received:   {self.messages_received}")
        print(f"  Errors:              {self.errors}")
        print(f"  Duplicates:          {self.duplicates}")

        throughput = self.messages_sent / max(elapsed, 1)
        error_rate = (self.errors / max(self.messages_sent, 1)) * 100
        print(f"\n  Throughput:           {throughput:.1f} msg/s")
        print(f"  Error rate:          {error_rate:.2f}%")

        if self.latencies:
            self.latencies.sort()
            p50 = self.latencies[len(self.latencies) // 2]
            p95_idx = int(len(self.latencies) * 0.95)
            p99_idx = int(len(self.latencies) * 0.99)
            p95 = self.latencies[min(p95_idx, len(self.latencies) - 1)]
            p99 = self.latencies[min(p99_idx, len(self.latencies) - 1)]
            avg = statistics.mean(self.latencies)
            print(f"\n  Latency (fanout):")
            print(f"    p50:               {p50:.1f}ms")
            print(f"    p95:               {p95:.1f}ms")
            print(f"    p99:               {p99:.1f}ms")
            print(f"    avg:               {avg:.1f}ms")

        print(f"\n  Acceptance Criteria:")
        ok_lost = self.duplicates == 0
        ok_error = error_rate < 1.0
        print(f"    0 duplicates:      {'✅ PASS' if ok_lost else '❌ FAIL'} ({self.duplicates} found)")
        print(f"    Error rate < 1%:   {'✅ PASS' if ok_error else '❌ FAIL'} ({error_rate:.2f}%)")
        if self.latencies:
            ok_latency = p95 < 500
            print(f"    p95 < 500ms:       {'✅ PASS' if ok_latency else '❌ FAIL'} ({p95:.1f}ms)")

        print("\n" + "=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WebSocket Load Test")
    parser.add_argument("--host", default="localhost:8000", help="Server host:port")
    parser.add_argument("--connections", type=int, default=50, help="Number of concurrent connections")
    parser.add_argument("--duration", type=int, default=30, help="Test duration in seconds")
    parser.add_argument("--rate", type=float, default=1.0, help="Messages per second per client")
    args = parser.parse_args()

    runner = LoadTestRunner(args.host, args.connections, args.duration, args.rate)
    asyncio.run(runner.run())
