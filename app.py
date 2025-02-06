from fastapi import FastAPI, WebSocket, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

import websockets
import numpy as np

import ssl
import certifi
import asyncio
import json
import time
from collections import deque, defaultdict
from hashlib import sha256
from datetime import datetime, timedelta
from typing import List, Optional


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)


class RateLimiter:
    def __init__(self, max_requests: int, time_window: int):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = defaultdict(list)

    async def is_allowed(self, client_id: str) -> bool:
        now = time.time()
        self.requests[client_id] = [req_time for req_time in self.requests[client_id] 
                                  if now - req_time < self.time_window]

        if len(self.requests[client_id]) >= self.max_requests:
            return False

        self.requests[client_id].append(now)
        return True


class RandomNumberGenerator:
    def __init__(self, buffer_size=1000):
        self.buffer_size = buffer_size
        self.time_buffer = deque(maxlen=buffer_size)
        self.state_buffer = deque(maxlen=buffer_size)
        self.last_timestamp = None
        self.entropy_pool = 0

    async def process_message(self, timestamp, value):
        """Process each incoming websocket message"""
        current_time = time.time_ns()

        self.time_buffer.append(current_time)
        self.state_buffer.append(value)

        if self.last_timestamp:
            interval = current_time - self.last_timestamp
            self.entropy_pool ^= interval

        self.last_timestamp = current_time

    def generate_random_integers(self, n=10, min_val=0, max_val=10):
        """Generate a random number using current entropy pool"""
        if len(self.time_buffer) < 10:
            raise ValueError("Not enough entropy collected")

        random_integers = []

        for _ in range(n):
            entropy_sources = [
                np.array([t % 1_000_000 for t in self.time_buffer]),
                np.array(self.state_buffer),
                np.array([self.entropy_pool]),
                np.array([time.time_ns()]),
                np.array([_])
            ]

            combined = np.concatenate([arr.flatten() for arr in entropy_sources])
            hash_value = int(sha256(combined.tobytes()).hexdigest(), 16)

            range_size = max_val - min_val + 1
            random_int = min_val + (hash_value % range_size)

            random_integers.append(random_int)

        return random_integers

    def get_entropy_estimate(self):
        """Estimate current entropy in bits"""
        if len(self.time_buffer) < 2:
            return 0

        intervals = np.diff(np.array(self.time_buffer))
        _, counts = np.unique(intervals, return_counts=True)
        probabilities = counts / len(intervals)
        entropy = -np.sum(probabilities * np.log2(probabilities))
        return entropy


rng = RandomNumberGenerator()
rate_limiter = RateLimiter(max_requests=10, time_window=60)


@app.get("/integers")
async def get_random(
    n: Optional[int] = Query(default=10, ge=1, le=1000, description="Number of integers to generate"),
    min_val: Optional[int] = Query(default=0, description="Minimum value (inclusive)"),
    max_val: Optional[int] = Query(default=10, description="Maximum value (inclusive)")
):
    client_id = request.client.host

    if not await rate_limiter.is_allowed(client_id):
        raise HTTPException(status_code=429, detail="Too many requests")

    try:
        if max_val <= min_val:
            raise HTTPException(
                status_code=400, 
                detail="max_val must be greater than min_val"
            )

        value = rng.generate_random_integers(n, min_val, max_val)
        entropy = rng.get_entropy_estimate()

        return {
            "integers": value,
            "timestamp": datetime.now().isoformat(),
            "entropy": entropy,
            "count": len(value),
            "range": {"min": min_val, "max": max_val}
        }
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))


async def connect_to_source():
    """Background task to connect to the source WebSocket"""
    while True:
        try:
            async with websockets.connect(
                'wss://globalcapslock.com/ws',
                ssl=ssl.SSLContext() 
            ) as ws:
                while True:
                    message = await ws.recv()
                    await rng.process_message(datetime.now(), message)
        except Exception as e:
            print(f"Source connection error: {e}")
            await asyncio.sleep(1)



@app.on_event("startup")
async def startup():
    asyncio.create_task(connect_to_source())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
