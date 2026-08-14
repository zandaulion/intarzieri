"""Romanian train delay API.

Two upstream surfaces, both public and both rate-limited here:

  * the live map, cached lazily -- fetched only when someone asks and the
    cached copy is older than POLL_SECONDS, so an idle server is silent;
  * per-train itineraries, fetched only for trains somebody is watching,
    grouped so N subscribers to one train cost one fetch.

Trip subscriptions turn those itineraries into Web Push notifications for
departure, delay changes and arrival.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import orjson
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import ORJSONResponse

import iris
import push
import route as R
import trips

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "120"))
WATCH_SECONDS = int(os.getenv("WATCH_SECONDS", "180"))
ROUTE_TTL = int(os.getenv("ROUTE_TTL", "60"))
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


class RouteCache:
    """Short-lived cache in front of the itinerary endpoint.

    Each fetch is a GET (for the antiforgery token) plus a POST, and the token
    is bound to a cookie, so the two must not interleave -- hence the lock.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._entries: dict[tuple[str, str], tuple[float, R.Route]] = {}

    async def get(self, client: httpx.AsyncClient, number: str, when: date) -> R.Route:
        key = (number, when.isoformat())
        hit = self._entries.get(key)
        now = asyncio.get_running_loop().time()
        if hit and now - hit[0] < ROUTE_TTL:
            return hit[1]
        async with self._lock:
            hit = self._entries.get(key)
            now = asyncio.get_running_loop().time()
            if hit and now - hit[0] < ROUTE_TTL:
                return hit[1]
            rt = await R.fetch_route(client, number, when)
            self._entries[key] = (now, rt)
            if len(self._entries) > 200:
                oldest = sorted(self._entries.items(), key=lambda kv: kv[1][0])[:50]
                for k, _ in oldest:
                    self._entries.pop(k, None)
            return rt


routes = RouteCache()
_map_lock = asyncio.Lock()
_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    assert _client is not None
    return _client


async def ensure_map(force: bool = False) -> None:
    """Refresh the live-map snapshot if it is older than POLL_SECONDS."""
    if not force and store.fetched_at is not None:
        age = (datetime.now(timezone.utc) - store.fetched_at).total_seconds()
        if age < POLL_SECONDS:
            return
    async with _map_lock:
        if not force and store.fetched_at is not None:
            age = (datetime.now(timezone.utc) - store.fetched_at).total_seconds()
            if age < POLL_SECONDS:
                return
        try:
            await poll_once(client())
        except Exception as exc:  # keep serving the previous snapshot
            store.consecutive_failures += 1
            store.last_error = f"{type(exc).__name__}: {exc}"
            log.warning(
                "map fetch failed (%d in a row): %s", store.consecutive_failures, exc
            )


async def watcher() -> None:
    """Drives trip notifications. Sleeps cheaply when nobody is watching."""
    while True:
        try:
            result = await trips.watch_once(client())
            if result["polled"] or result["events_sent"]:
                log.info("watch pass: %s", result)
            app.state.last_watch = result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("watch pass failed: %s", exc)
        await asyncio.sleep(WATCH_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    trips.init()
    app.state.last_watch = None
    _client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=UPSTREAM_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
    )
    task = asyncio.create_task(watcher())
    try:
        yield
    finally:
        task.cancel()
        await _client.aclose()


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
    ok = store.consecutive_failures < 5
    return {
        "ok": ok,
        "last_error": store.last_error,
        "last_watch": getattr(app.state, "last_watch", None),
        **_meta(),
    }


@app.get("/api/trains")
async def trains(
    delayed_only: bool = Query(False),
    min_delay: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    await ensure_map()
    items = store.trains
    if delayed_only or min_delay:
        items = [t for t in items if t.delay_min >= max(min_delay, 1)]
    items = sorted(items, key=lambda t: -t.delay_min)[:limit]
    return {"meta": _meta(), "trains": [shape(t) for t in items]}


@app.get("/api/train/{number}")
async def train(number: str):
    await ensure_map()
    key = number.lstrip("0") or number
    t = store.by_number.get(key) or store.by_number.get(number)
    if not t:
        raise HTTPException(
            status_code=404,
            detail=f"Train {number} is not currently on the live map. "
                   "Only trains running right now appear here.",
        )
    return {"meta": _meta(), "train": shape(t)}


# --------------------------------------------------------------------------
# itinerary + trip subscriptions
# --------------------------------------------------------------------------
@app.get("/api/route/{number}")
async def train_route(number: str, when: str | None = Query(None, alias="date")):
    """The station list for one train, with live per-station delays."""
    try:
        day = date.fromisoformat(when) if when else datetime.now(R.RO).date()
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")

    try:
        rt = await routes.get(client(), number, day)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"upstream unavailable: {exc}")

    start = R.parse_ro_date(rt.date)

    def shape_branch(b: R.Branch) -> dict:
        stops = []
        for st in b.stops:
            d = st.dict()
            arr = R.actual_dt(start, st.arr_scheduled, st.arr_delay, st.arr_day_offset)
            dep = R.actual_dt(start, st.dep_scheduled, st.dep_delay, st.dep_day_offset)
            d["arr_expected"] = arr.isoformat() if arr else None
            d["dep_expected"] = dep.isoformat() if dep else None
            stops.append(d)
        return {
            "code": b.code,
            "name": b.name,
            "is_default": b.is_default,
            "summary_delay": b.summary_delay,
            "reported_at": b.reported_at,
            "measured_at": b.measured_at,
            "measured_kind": b.measured_kind,
            "position_note": b.position_note,
            "between": b.between,
            "stops": stops,
        }

    return {
        "number": rt.number,
        "category": rt.category,
        "run_date": start.isoformat(),
        # A train may be published as several variants of the same run; the
        # default one is what InfoFer shows first.
        "branches": [shape_branch(b) for b in rt.branches],
    }


@app.get("/api/vapid")
async def vapid_key():
    return {"publicKey": push.vapid.public_key}


@app.post("/api/trips")
async def create_trip(payload: dict = Body(...)):
    sub = payload.get("subscription") or {}
    if not sub.get("endpoint") or not (sub.get("keys") or {}).get("auth"):
        raise HTTPException(400, "a complete push subscription is required")

    number = str(payload.get("number") or "").strip()
    from_slug = payload.get("from_slug")
    to_slug = payload.get("to_slug")
    if not number or not from_slug or not to_slug:
        raise HTTPException(400, "number, from_slug and to_slug are required")
    if from_slug == to_slug:
        raise HTTPException(400, "departure and arrival must differ")

    try:
        day = (
            date.fromisoformat(payload["run_date"])
            if payload.get("run_date")
            else datetime.now(R.RO).date()
        )
    except (ValueError, TypeError):
        raise HTTPException(400, "run_date must be YYYY-MM-DD")

    try:
        rt = await routes.get(client(), number, day)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"upstream unavailable: {exc}")

    branch = rt.branch_for(from_slug, to_slug)
    if branch is None:
        on_any = any(
            st.slug in (from_slug, to_slug) for b in rt.branches for st in b.stops
        )
        raise HTTPException(
            400,
            "the arrival station comes before the departure one"
            if on_any
            else "those stations are not on this train's route",
        )

    index = {st.slug: i for i, st in enumerate(branch.stops)}
    start = R.parse_ro_date(rt.date)
    src = branch.stops[index[from_slug]]
    dst = branch.stops[index[to_slug]]
    dep_planned = R.actual_dt(start, src.dep_scheduled, 0, src.dep_day_offset)
    arr_planned = R.actual_dt(start, dst.arr_scheduled, 0, dst.arr_day_offset)

    try:
        trip_id = await trips.add_trip(
            sub,
            {
                "number": rt.number,
                "run_date": start.isoformat(),
                "from_slug": from_slug,
                "from_name": src.name,
                "to_slug": to_slug,
                "to_name": dst.name,
                "dep_planned": dep_planned.isoformat() if dep_planned else None,
                "arr_planned": arr_planned.isoformat() if arr_planned else None,
                "branch_code": branch.code,
            },
        )
    except trips.TripLimitReached as exc:
        raise HTTPException(
            409,
            f"You can watch {exc.limit} trains at once. "
            "Stop watching one before adding another.",
        )

    await trips.prime(trip_id, rt)

    return {
        "id": trip_id,
        "number": rt.number,
        "category": rt.category,
        "run_date": start.isoformat(),
        "from_name": src.name,
        "to_name": dst.name,
        "dep_planned": dep_planned.isoformat() if dep_planned else None,
        "arr_planned": arr_planned.isoformat() if arr_planned else None,
    }


@app.post("/api/trips/list")
async def list_trips(payload: dict = Body(...)):
    """POST, not GET: the endpoint URL is a credential and does not belong
    in a query string that ends up in logs."""
    endpoint = (payload.get("subscription") or {}).get("endpoint") or payload.get("endpoint")
    if not endpoint:
        raise HTTPException(400, "subscription endpoint required")
    return {
        "trips": await trips.list_trips(endpoint),
        "active": await trips.count_active(endpoint),
        "limit": trips.MAX_ACTIVE,
    }


@app.post("/api/trips/{trip_id}/delete")
async def remove_trip(trip_id: int, payload: dict = Body(...)):
    endpoint = (payload.get("subscription") or {}).get("endpoint") or payload.get("endpoint")
    if not endpoint:
        raise HTTPException(400, "subscription endpoint required")
    if not await trips.delete_trip(trip_id, endpoint):
        raise HTTPException(404, "no such trip for this subscription")
    return {"deleted": trip_id}


@app.post("/api/push/test")
async def push_test(payload: dict = Body(...)):
    sub = payload.get("subscription") or {}
    if not sub.get("endpoint"):
        raise HTTPException(400, "subscription required")
    ok, status = await push.send(
        sub,
        {
            "title": "Notifications are working",
            "body": "You will get one of these when your train departs, "
                    "when its delay changes, and when it arrives.",
            "tag": "test",
            "kind": "test",
        },
    )
    return {"delivered": ok, "status": status}
