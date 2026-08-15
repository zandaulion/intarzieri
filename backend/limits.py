"""Rate limits on the things a user can do that reach CFR.

The watcher is bounded by design: trips are capped per device, grouped by
train, and polled on a fixed interval. Interactive lookups are not -- a
device can ask for an unbounded number of distinct train numbers, and each
miss is a fetch. These buckets put a ceiling on that, per device so one
person cannot spoil it for everyone, and globally so the total is bounded
regardless of how many devices exist.
"""
from __future__ import annotations

import os
import time


class RateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"rate limited, retry in {retry_after}s")
        self.retry_after = retry_after


class Bucket:
    """Classic token bucket. `capacity` is the burst, `per_hour` the refill."""

    def __init__(self, capacity: int, per_hour: int) -> None:
        self.capacity = float(capacity)
        self.rate = per_hour / 3600.0
        self.tokens = float(capacity)
        self.updated = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def take(self, n: float = 1.0) -> None:
        self._refill()
        if self.tokens < n:
            short = n - self.tokens
            raise RateLimited(max(1, int(short / self.rate) + 1))
        self.tokens -= n

    def peek(self) -> float:
        self._refill()
        return self.tokens


# Interactive route lookups. Generous for a person looking up their train,
# tight enough that a script cannot walk the timetable.
DEVICE_LOOKUPS = Bucket(
    int(os.getenv("LOOKUP_BURST", "8")), int(os.getenv("LOOKUP_PER_HOUR", "20"))
)
# Sized so several people can each use their full per-device burst at once
# without tripping it, while still capping the total if devices multiply.
GLOBAL_LOOKUPS = Bucket(
    int(os.getenv("GLOBAL_LOOKUP_BURST", "30")),
    int(os.getenv("GLOBAL_LOOKUP_PER_HOUR", "150")),
)
# Churn: adding and removing the same trip repeatedly is cheap for us but
# costs a fetch each time it primes.
DEVICE_TRIPS = Bucket(
    int(os.getenv("TRIP_BURST", "10")), int(os.getenv("TRIP_PER_HOUR", "15"))
)

_per_device: dict[tuple[str, int], Bucket] = {}
_TEMPLATES = {"lookup": DEVICE_LOOKUPS, "trip": DEVICE_TRIPS}


def _bucket(kind: str, device_id: int) -> Bucket:
    key = (kind, device_id)
    b = _per_device.get(key)
    if b is None:
        t = _TEMPLATES[kind]
        b = Bucket(int(t.capacity), int(t.rate * 3600))
        _per_device[key] = b
        if len(_per_device) > 500:                 # devices are few; stay tidy
            for k, v in sorted(_per_device.items(), key=lambda kv: kv[1].updated)[:100]:
                if v.peek() >= v.capacity:         # only drop fully-refilled ones
                    _per_device.pop(k, None)
    return b


def take_lookup(device_id: int) -> None:
    """One upstream itinerary fetch on behalf of this device."""
    _bucket("lookup", device_id).take()
    try:
        GLOBAL_LOOKUPS.take()
    except RateLimited:
        # Refund: the device did nothing wrong, we hit our own ceiling.
        b = _bucket("lookup", device_id)
        b.tokens = min(b.capacity, b.tokens + 1)
        raise


def take_trip(device_id: int) -> None:
    _bucket("trip", device_id).take()


def snapshot() -> dict:
    return {
        "global_lookup_tokens": round(GLOBAL_LOOKUPS.peek(), 1),
        "global_lookup_capacity": int(GLOBAL_LOOKUPS.capacity),
        "tracked_devices": len({d for _, d in _per_device}),
    }
