"""Trip subscriptions: storage, event detection, notification dispatch.

A "trip" is one user watching one train between two stations they chose. The
watcher polls only trains that somebody is actually watching, and only inside
that train's journey window, so upstream load scales with real usage rather
than with the timetable.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path


import route as R

log = logging.getLogger("trains.trips")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "trips.db"

# How much the delay must move before it is worth waking somebody's phone.
DELAY_THRESHOLD = int(os.getenv("DELAY_THRESHOLD", "5"))
# Start watching this long before scheduled departure, stop this long after
# the expected arrival.
LEAD_MINUTES = int(os.getenv("LEAD_MINUTES", "45"))
TRAIL_MINUTES = int(os.getenv("TRAIL_MINUTES", "30"))
# Hard stop for watching an overdue train. Generous, because the window is
# computed from *scheduled* times and a train we have no live delay for yet
# could legitimately be hours behind.
MAX_OVERDUE = timedelta(hours=int(os.getenv("MAX_OVERDUE_HOURS", "6")))
# A finished trip stays in the user's list this long -- long enough that this
# morning's train is still there, short enough that last week's is not.
LIST_KEEP = timedelta(hours=int(os.getenv("LIST_KEEP_HOURS", "12")))
# ...and is deleted outright this long after it finished, so the table cannot
# grow without bound.
PURGE_AFTER = timedelta(hours=int(os.getenv("PURGE_AFTER_HOURS", "48")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS push_subs (
    id         INTEGER PRIMARY KEY,
    endpoint   TEXT UNIQUE NOT NULL,
    p256dh     TEXT NOT NULL,
    auth       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trips (
    id          INTEGER PRIMARY KEY,
    sub_id      INTEGER NOT NULL REFERENCES push_subs(id) ON DELETE CASCADE,
    number      TEXT NOT NULL,
    run_date    TEXT NOT NULL,          -- YYYY-MM-DD, the route's start date
    from_slug   TEXT NOT NULL,
    from_name   TEXT NOT NULL,
    to_slug     TEXT NOT NULL,
    to_name     TEXT NOT NULL,
    branch_code TEXT,                   -- which published variant of the run
    created_at  TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    departed    INTEGER NOT NULL DEFAULT 0,
    arrived     INTEGER NOT NULL DEFAULT 0,
    last_delay  INTEGER,                -- delay we last told the user about
    last_event  TEXT,
    -- Planned times captured at subscribe time, so the watcher can decide
    -- whether a trip is worth fetching without first fetching it.
    dep_planned TEXT,
    arr_planned TEXT
);
CREATE INDEX IF NOT EXISTS trips_active ON trips(active, number, run_date);
"""


@contextmanager
def _connect():
    """`with sqlite3.connect(...)` commits but does *not* close, so the
    connection is closed explicitly here. WAL is set once in init(), not per
    connection -- switching journal mode takes a lock every time.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init() -> None:
    with _connect() as con:
        con.execute("PRAGMA journal_mode = WAL")
        con.executescript(SCHEMA)
        # Additive migrations for databases created by an earlier version.
        have = {r["name"] for r in con.execute("PRAGMA table_info(trips)")}
        for col, decl in (("branch_code", "TEXT"),):
            if col not in have:
                con.execute(f"ALTER TABLE trips ADD COLUMN {col} {decl}")
                log.info("migrated trips: added %s", col)


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------
def _upsert_sub(con: sqlite3.Connection, sub: dict) -> int:
    keys = sub.get("keys") or {}
    endpoint = sub["endpoint"]
    now = datetime.now().isoformat(timespec="seconds")
    con.execute(
        """INSERT INTO push_subs (endpoint, p256dh, auth, created_at)
           VALUES (?,?,?,?)
           ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh,
                                               auth=excluded.auth""",
        (endpoint, keys.get("p256dh", ""), keys.get("auth", ""), now),
    )
    row = con.execute("SELECT id FROM push_subs WHERE endpoint=?", (endpoint,)).fetchone()
    return row["id"]


def _add_trip_blocking(sub: dict, trip: dict) -> int:
    with _connect() as con:
        sub_id = _upsert_sub(con, sub)
        cur = con.execute(
            """INSERT INTO trips (sub_id, number, run_date, from_slug, from_name,
                                  to_slug, to_name, created_at,
                                  dep_planned, arr_planned, branch_code)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sub_id,
                trip["number"],
                trip["run_date"],
                trip["from_slug"],
                trip["from_name"],
                trip["to_slug"],
                trip["to_name"],
                datetime.now().isoformat(timespec="seconds"),
                trip.get("dep_planned"),
                trip.get("arr_planned"),
                trip.get("branch_code"),
            ),
        )
        return cur.lastrowid


async def add_trip(sub: dict, trip: dict) -> int:
    return await asyncio.to_thread(_add_trip_blocking, sub, trip)


def _finished_at(trip: dict) -> datetime | None:
    """When a retired trip stopped being interesting."""
    return _iso(trip.get("last_event")) or _iso(trip.get("arr_planned"))


def _list_trips_blocking(endpoint: str) -> list[dict]:
    with _connect() as con:
        rows = [
            dict(r)
            for r in con.execute(
                """SELECT t.* FROM trips t JOIN push_subs s ON s.id = t.sub_id
                   WHERE s.endpoint = ? ORDER BY t.id DESC""",
                (endpoint,),
            )
        ]
    # Filtered here rather than in SQL: arr_planned is an ISO string whose UTC
    # offset shifts with DST, so lexical comparison is not safe.
    now = datetime.now(R.RO)
    kept = []
    for r in rows:
        if r["active"]:
            kept.append(r)
            continue
        end = _finished_at(r)
        if end is None or now - end <= LIST_KEEP:
            kept.append(r)
    return kept


async def list_trips(endpoint: str) -> list[dict]:
    return await asyncio.to_thread(_list_trips_blocking, endpoint)


def _delete_trip_blocking(trip_id: int, endpoint: str) -> bool:
    with _connect() as con:
        cur = con.execute(
            """DELETE FROM trips WHERE id = ? AND sub_id =
               (SELECT id FROM push_subs WHERE endpoint = ?)""",
            (trip_id, endpoint),
        )
        return cur.rowcount > 0


async def delete_trip(trip_id: int, endpoint: str) -> bool:
    return await asyncio.to_thread(_delete_trip_blocking, trip_id, endpoint)


def _active_blocking() -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            """SELECT t.*, s.endpoint, s.p256dh, s.auth
               FROM trips t JOIN push_subs s ON s.id = t.sub_id
               WHERE t.active = 1"""
        ).fetchall()
        return [dict(r) for r in rows]


def _update_blocking(trip_id: int, fields: dict) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    with _connect() as con:
        con.execute(
            f"UPDATE trips SET {sets} WHERE id = ?", (*fields.values(), trip_id)
        )


def _drop_sub_blocking(endpoint: str) -> None:
    with _connect() as con:
        con.execute("DELETE FROM push_subs WHERE endpoint = ?", (endpoint,))


# --------------------------------------------------------------------------
# event detection
# --------------------------------------------------------------------------
def _find(stops: list[R.Stop], slug: str) -> tuple[int, R.Stop] | None:
    for i, s in enumerate(stops):
        if s.slug == slug:
            return i, s
    return None


def _branch_of(trip: dict, rt: R.Route):
    """The branch this trip was booked on, by code, falling back to whichever
    branch still contains the leg (InfoFer renumbers branch codes daily)."""
    code = trip.get("branch_code")
    if code:
        for b in rt.branches:
            if b.code == code:
                return b
    return rt.branch_for(trip["from_slug"], trip["to_slug"])


def _fmt(dt: datetime | None) -> str | None:
    return dt.strftime("%H:%M") if dt else None


def evaluate(trip: dict, rt: R.Route, now: datetime) -> tuple[list[dict], dict]:
    """-> (events to send, column updates).

    InfoFer publishes no explicit "has departed" flag, so a station counts as
    passed once its scheduled time plus the currently reported delay is in the
    past. That is the same arithmetic a passenger on the platform does.
    """
    events: list[dict] = []
    updates: dict = {}

    start = R.parse_ro_date(rt.date)
    branch = _branch_of(trip, rt)
    if branch is None:
        log.warning("trip %s: leg no longer on any published branch", trip["id"])
        return events, {"active": 0}
    src = _find(branch.stops, trip["from_slug"])
    dst = _find(branch.stops, trip["to_slug"])
    if not src or not dst:
        log.warning("trip %s: stations no longer on route", trip["id"])
        return events, {"active": 0}

    _, from_stop = src
    _, to_stop = dst

    dep_dt = R.actual_dt(
        start, from_stop.dep_scheduled or from_stop.arr_scheduled,
        from_stop.dep_delay if from_stop.dep_scheduled else from_stop.arr_delay,
        from_stop.dep_day_offset if from_stop.dep_scheduled else from_stop.arr_day_offset,
    )
    arr_dt = R.actual_dt(
        start, to_stop.arr_scheduled or to_stop.dep_scheduled,
        to_stop.arr_delay if to_stop.arr_scheduled else to_stop.dep_delay,
        to_stop.arr_day_offset if to_stop.arr_scheduled else to_stop.dep_day_offset,
    )
    arr_delay = to_stop.arr_delay if to_stop.arr_delay is not None else to_stop.dep_delay
    label = f"{rt.category or ''} {rt.number}".strip()

    # 1. departure
    if not trip["departed"] and dep_dt and now >= dep_dt:
        dep_delay = from_stop.dep_delay or 0
        late = f"{dep_delay:+d} min" if dep_delay else "on time"
        events.append({
            "kind": "departed",
            "title": f"{label} departed {trip['from_name']}",
            "body": f"Left at {_fmt(dep_dt)} ({late}). "
                    f"Expected in {trip['to_name']} at {_fmt(arr_dt)}.",
            "tag": f"trip-{trip['id']}-departed",
        })
        updates["departed"] = 1
        if trip["last_delay"] is None and arr_delay is not None:
            updates["last_delay"] = arr_delay

    # 2. delay change at the destination
    if arr_delay is not None and not trip["arrived"]:
        known = trip["last_delay"] if "last_delay" not in updates else updates["last_delay"]
        if known is None:
            updates["last_delay"] = arr_delay
        elif abs(arr_delay - known) >= DELAY_THRESHOLD:
            direction = "increased" if arr_delay > known else "recovered"
            events.append({
                "kind": "delay",
                "title": f"{label}: delay {direction} to {arr_delay} min",
                "body": f"Now expected in {trip['to_name']} at {_fmt(arr_dt)} "
                        f"(was {known:+d} min, now {arr_delay:+d} min).",
                "tag": f"trip-{trip['id']}-delay",
            })
            updates["last_delay"] = arr_delay

    # 3. arrival
    if not trip["arrived"] and arr_dt and now >= arr_dt:
        d = arr_delay or 0
        late = f"{d} min late" if d > 0 else "on time"
        events.append({
            "kind": "arrived",
            "title": f"{label} arrived in {trip['to_name']}",
            "body": f"Arrived at {_fmt(arr_dt)} ({late}).",
            "tag": f"trip-{trip['id']}-arrived",
        })
        updates["arrived"] = 1
        updates["active"] = 0

    # Retire trips whose window has closed even if we never saw an arrival,
    # so a train that vanishes upstream cannot be watched forever.
    if arr_dt and now > arr_dt + timedelta(hours=6):
        updates["active"] = 0

    if events:
        updates["last_event"] = now.isoformat(timespec="seconds")
    return events, updates


def window_open(trip: dict, now: datetime) -> bool:
    """Is this trip close enough in time to be worth an upstream fetch?

    Uses times captured when the user subscribed, widened by the delay we
    already know about, so a train running two hours late keeps being polled.
    """
    dep = _iso(trip.get("dep_planned"))
    arr = _iso(trip.get("arr_planned"))
    if dep and now < dep - timedelta(minutes=LEAD_MINUTES):
        return False
    if arr:
        slack = timedelta(minutes=(trip.get("last_delay") or 0) + TRAIL_MINUTES)
        if now > arr + slack + MAX_OVERDUE:
            return False
    return True


def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=R.RO)


# --------------------------------------------------------------------------
# watcher
# --------------------------------------------------------------------------
async def _dispatch(trip: dict, events: list[dict]) -> None:
    import push

    sub = {
        "endpoint": trip["endpoint"],
        "keys": {"p256dh": trip["p256dh"], "auth": trip["auth"]},
    }
    for ev in events:
        payload = {
            "title": ev["title"],
            "body": ev["body"],
            "tag": ev["tag"],
            "kind": ev["kind"],
            "trip_id": trip["id"],
            "number": trip["number"],
        }
        ok, status = await push.send(sub, payload)
        if not ok and status in push.DEAD:
            await asyncio.to_thread(_drop_sub_blocking, trip["endpoint"])
            return
        log.info(
            "trip %s: sent %s to %s (ok=%s)",
            trip["id"], ev["kind"], trip["endpoint"][:48], ok,
        )


def _get_trip_blocking(trip_id: int) -> dict | None:
    with _connect() as con:
        row = con.execute(
            """SELECT t.*, s.endpoint, s.p256dh, s.auth
               FROM trips t JOIN push_subs s ON s.id = t.sub_id
               WHERE t.id = ?""",
            (trip_id,),
        ).fetchone()
        return dict(row) if row else None


async def prime(trip_id: int, rt: R.Route) -> None:
    """Adopt the train's current state at subscribe time without notifying.

    Someone who subscribes to a train that already left should not be told it
    departed two hours ago; they should just start getting updates from now on.
    """
    trip = await asyncio.to_thread(_get_trip_blocking, trip_id)
    if not trip:
        return
    _, updates = evaluate(trip, rt, datetime.now(R.RO))
    updates.pop("last_event", None)
    # An arrival detected at subscribe time still retires the trip -- there is
    # nothing left to watch -- but silently.
    if updates:
        await asyncio.to_thread(_update_blocking, trip_id, updates)


def _purge_blocking() -> int:
    """Drop trips that finished long ago. Only retired trips are considered,
    so an overdue train still being watched is never removed."""
    now = datetime.now(R.RO)
    with _connect() as con:
        rows = [
            dict(r)
            for r in con.execute(
                "SELECT id, arr_planned, last_event FROM trips WHERE active = 0"
            )
        ]
        stale = [
            r["id"]
            for r in rows
            if (end := _finished_at(r)) is not None and now - end > PURGE_AFTER
        ]
        if stale:
            con.executemany(
                "DELETE FROM trips WHERE id = ?", [(i,) for i in stale]
            )
    return len(stale)


async def watch_once(client) -> dict:
    """One pass over every active trip. Groups by train so N users watching
    the same train cost one upstream fetch, not N."""
    now = datetime.now(R.RO)
    active = await asyncio.to_thread(_active_blocking)

    due: dict[tuple[str, str], list[dict]] = {}
    for trip in active:
        if window_open(trip, now):
            due.setdefault((trip["number"], trip["run_date"]), []).append(trip)

    sent = 0
    errors = 0
    for (number, run_date), group in due.items():
        try:
            when = date.fromisoformat(run_date)
            rt = await R.fetch_route(client, number, when)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            log.warning("watch %s/%s failed: %s", number, run_date, exc)
            continue

        for trip in group:
            try:
                events, updates = evaluate(trip, rt, now)
            except Exception as exc:  # noqa: BLE001
                errors += 1
                log.warning("evaluate trip %s failed: %s", trip["id"], exc)
                continue
            if events:
                await _dispatch(trip, events)
                sent += len(events)
            if updates:
                await asyncio.to_thread(_update_blocking, trip["id"], updates)

    purged = await asyncio.to_thread(_purge_blocking)
    if purged:
        log.info("purged %d finished trip(s)", purged)

    return {
        "active": len(active),
        "polled": len(due),
        "events_sent": sent,
        "errors": errors,
        "purged": purged,
        "at": now.isoformat(timespec="seconds"),
    }
