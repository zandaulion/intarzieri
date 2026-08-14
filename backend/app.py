"""Romanian train delay API.

Polls the public live-map endpoint on a fixed interval and serves the parsed
result from memory. Client requests never trigger an upstream fetch, so load
on the source is constant (one request per POLL_SECONDS) no matter how many
people use this.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
import orjson
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import ORJSONResponse

import iris

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "120"))
UPSTREAM_TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT", "45"))
USER_AGENT = os.getenv(
    "USER_AGENT",
    "a1-train-tracker/1.0 (personal self-hosted dashboard; low-rate polling)",
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("trains")

STATIONS = orjson.loads((Path(__file__).parent / "stations.json").read_bytes())


class Store:
    """Latest good snapshot. Never cleared on failure -- stale beats empty."""

    def __init__(self) -> None:
        self.trains: list[iris.Train] = []
        self.by_number: dict[str, iris.Train] = {}
        self.fetched_at: datetime | None = None
        self.last_error: str | None = None
        self.consecutive_failures = 0

    def update(self, trains: list[iris.Train]) -> None:
        self.trains = trains
        self.by_number = {t.number: t for t in trains}
        self.fetched_at = datetime.now(timezone.utc)
        self.last_error = None
        self.consecutive_failures = 0


store = Store()


def nearest_station(lat: float, lon: float) -> dict | None:
    best, best_km = None, 1e9
    for s in STATIONS:
        km = iris.haversine_km(lat, lon, s["lat"], s["lon"])
        if km < best_km:
            best, best_km = s, km
    if best is None:
        return None
    return {
        "name": best["n"],
        "is_halt": bool(best["h"]),
        "lat": best["lat"],
        "lon": best["lon"],
        "distance_km": round(best_km, 2),
    }


def shape(t: iris.Train) -> dict:
    near = nearest_station(t.lat, t.lon)
    if t.on_time or t.delay_min == 0:
        status = "on_time"
    elif t.delay_min < 15:
        status = "slight"
    elif t.delay_min < 60:
        status = "delayed"
    else:
        status = "severe"
    return {
        "number": t.number,
        "category": t.category,
        "delay_min": t.delay_min,
        "status": status,
        "position": {"lat": t.lat, "lon": t.lon, "source": t.position_source},
        "measured": {
            "at": t.reported_at,
            "kind": t.report_kind,
            "minutes_ago": t.minutes_since_report,
            "near_station": near,
        },
    }


async def poll_once(client: httpx.AsyncClient) -> None:
    r = await client.get(
        iris.MAP_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": iris.REFERER,
            "X-Requested-With": "XMLHttpRequest",
            "Accept-Encoding": "gzip, deflate",
        },
        timeout=UPSTREAM_TIMEOUT,
    )
    r.raise_for_status()
    trains = iris.parse_map(r.text)
    if not trains:
        raise ValueError("upstream returned 0 trains -- page structure may have changed")
    store.update(trains)
    log.info("polled %d trains (%d bytes)", len(trains), len(r.content))


async def poller() -> None:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        while True:
            try:
                await poll_once(client)
            except Exception as exc:  # keep serving the previous snapshot
                store.consecutive_failures += 1
                store.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("poll failed (%d in a row): %s", store.consecutive_failures, exc)
            await asyncio.sleep(POLL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(poller())
    yield
    task.cancel()


app = FastAPI(title="RO Train Delays", default_response_class=ORJSONResponse, lifespan=lifespan)


def _meta() -> dict:
    return {
        "fetched_at": store.fetched_at.isoformat() if store.fetched_at else None,
        "age_seconds": int((datetime.now(timezone.utc) - store.fetched_at).total_seconds())
        if store.fetched_at else None,
        "poll_seconds": POLL_SECONDS,
        "train_count": len(store.trains),
        "stale": store.consecutive_failures > 0,
    }


@app.get("/api/health")
async def health():
    ok = bool(store.trains) and store.consecutive_failures < 5
    return {"ok": ok, "last_error": store.last_error, **_meta()}


@app.get("/api/trains")
async def trains(
    delayed_only: bool = Query(False),
    min_delay: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    items = store.trains
    if delayed_only or min_delay:
        items = [t for t in items if t.delay_min >= max(min_delay, 1)]
    items = sorted(items, key=lambda t: -t.delay_min)[:limit]
    return {"meta": _meta(), "trains": [shape(t) for t in items]}


@app.get("/api/train/{number}")
async def train(number: str):
    key = number.lstrip("0") or number
    t = store.by_number.get(key) or store.by_number.get(number)
    if not t:
        raise HTTPException(
            status_code=404,
            detail=f"Train {number} is not currently on the live map. "
                   "Only trains running right now appear here.",
        )
    return {"meta": _meta(), "train": shape(t)}
