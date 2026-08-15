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
from datetime import date, datetime, timedelta

import accounts
import route as R
from db import columns, connect

log = logging.getLogger("trains.trips")

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
# How many trains one subscription may watch at once. Only active trips
# count -- finished ones still sitting in the list, and ones awaiting purge,
# do not occupy a slot.
MAX_ACTIVE = int(os.getenv("MAX_ACTIVE_TRIPS", "5"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS push_subs (
    id         INTEGER PRIMARY KEY,
    device_id  INTEGER UNIQUE REFERENCES devices(id) ON DELETE CASCADE,
    endpoint   TEXT UNIQUE NOT NULL,
    p256dh     TEXT NOT NULL,
    auth       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trips (
    id          INTEGER PRIMARY KEY,
    device_id   INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
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
    arr_planned TEXT,
    -- Set on demand when the owner shares this trip. Reusable and multi-use:
    -- a family follows one link. Dies with the trip it points at.
    share_code  TEXT
);
"""

# Kept apart from the table definitions: an index on device_id cannot be
# created until the migration below has actually added that column to a
# pre-existing trips table.
INDEXES = """
CREATE INDEX IF NOT EXISTS trips_active ON trips(active, number, run_date);
CREATE INDEX IF NOT EXISTS trips_device ON trips(device_id);
-- ON CONFLICT(device_id) needs a unique index to target. A migrated database
-- got device_id via ALTER TABLE, which carries no constraint, so the index
-- has to be created explicitly rather than relying on the column definition.
CREATE UNIQUE INDEX IF NOT EXISTS push_subs_device ON push_subs(device_id);
-- Created explicitly: share_code reaches migrated databases via ALTER TABLE,
-- which in SQLite carries no constraint of its own.
CREATE UNIQUE INDEX IF NOT EXISTS trips_share ON trips(share_code)
    WHERE share_code IS NOT NULL;
"""


def init() -> list[int]:
    with connect() as con:
        con.execute("PRAGMA journal_mode = WAL")
        # devices must exist first: push_subs and trips reference it.
        accounts.init_schema(con)
        con.executescript(SCHEMA)
        for col, decl in (("branch_code", "TEXT"), ("share_code", "TEXT")):
            if col not in columns(con, "trips"):
                con.execute(f"ALTER TABLE trips ADD COLUMN {col} {decl}")
                log.info("migrated trips: added %s", col)
    adopted = _migrate_to_devices()
    with connect() as con:
        con.executescript(INDEXES)
        # push_subs.device_id came from an ALTER TABLE, which in SQLite cannot
        # carry ON DELETE CASCADE, so rows can outlive their device. Sweep any
        # that already have.
        orphans = con.execute(
            "DELETE FROM push_subs WHERE device_id IS NOT NULL AND device_id "
            "NOT IN (SELECT id FROM devices)"
        ).rowcount
        if orphans:
            log.info("removed %d push subscription(s) with no device", orphans)
    return adopted


def _migrate_to_devices() -> list[int]:
    """Move identity from the push endpoint onto a device row.

    Before this, a trip belonged to a push subscription -- so a browser
    rotating its endpoint silently orphaned every trip. Existing rows are
    adopted by freshly minted devices; the returned ids get an adoption
    invite so their owners keep what they were watching.
    """
    adopted: list[int] = []
    with connect() as con:
        con.execute("PRAGMA foreign_keys = OFF")

        if "device_id" not in columns(con, "push_subs"):
            con.execute("ALTER TABLE push_subs ADD COLUMN device_id INTEGER")
            log.info("migrated push_subs: added device_id")

        orphans = con.execute(
            "SELECT id FROM push_subs WHERE device_id IS NULL"
        ).fetchall()
        for row in orphans:
            token = accounts.new_device_token()
            cur = con.execute(
                "INSERT INTO devices (token_hash, label, created_at) VALUES (?,?,?)",
                (accounts._hash(token), "migrated", accounts._now()),
            )
            con.execute(
                "UPDATE push_subs SET device_id = ? WHERE id = ?", (cur.lastrowid, row["id"])
            )
            adopted.append(cur.lastrowid)
            log.info("migrated push_sub %s onto device %s", row["id"], cur.lastrowid)

        if "device_id" not in columns(con, "trips"):
            # SQLite cannot retype a column in place, so the table is rebuilt.
            log.info("rebuilding trips around device_id")
            con.execute("ALTER TABLE trips RENAME TO trips_old")
            con.executescript(SCHEMA)  # tables only; indexes follow in init()
            con.execute(
                """INSERT INTO trips (id, device_id, number, run_date, from_slug,
                        from_name, to_slug, to_name, created_at, active, departed,
                        arrived, last_delay, last_event, dep_planned, arr_planned,
                        branch_code)
                   SELECT o.id,
                          COALESCE(p.device_id, (SELECT MIN(id) FROM devices)),
                          o.number, o.run_date, o.from_slug, o.from_name,
                          o.to_slug, o.to_name, o.created_at, o.active, o.departed,
                          o.arrived, o.last_delay, o.last_event, o.dep_planned,
                          o.arr_planned, o.branch_code
                     FROM trips_old o LEFT JOIN push_subs p ON p.id = o.sub_id"""
            )
            moved = con.execute("SELECT COUNT(*) FROM trips").fetchone()[0]
            con.execute("DROP TABLE trips_old")
            log.info("rebuilt trips: %d row(s) carried over", moved)

        con.execute("PRAGMA foreign_keys = ON")
    return adopted


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------
def _upsert_sub(con, device_id: int, sub: dict) -> int:
    """One push subscription per device, replaced when the browser rotates it.

    Keyed on device_id rather than endpoint precisely so a rotation updates
    the row instead of stranding the device's trips behind a dead endpoint.
    """
    keys = sub.get("keys") or {}
    endpoint = sub["endpoint"]
    now = datetime.now().isoformat(timespec="seconds")
    # The same endpoint could previously have belonged to another device row.
    con.execute(
        "DELETE FROM push_subs WHERE endpoint = ? AND device_id IS NOT ?",
        (endpoint, device_id),
    )
    con.execute(
        """INSERT INTO push_subs (device_id, endpoint, p256dh, auth, created_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(device_id) DO UPDATE SET endpoint=excluded.endpoint,
                                                p256dh=excluded.p256dh,
                                                auth=excluded.auth""",
        (device_id, endpoint, keys.get("p256dh", ""), keys.get("auth", ""), now),
    )
    row = con.execute(
        "SELECT id FROM push_subs WHERE device_id = ?", (device_id,)
    ).fetchone()
    return row["id"]


def _save_sub_blocking(device_id: int, sub: dict) -> None:
    with connect() as con:
        _upsert_sub(con, device_id, sub)


async def save_subscription(device_id: int, sub: dict) -> None:
    await asyncio.to_thread(_save_sub_blocking, device_id, sub)


def _share_trip_blocking(trip_id: int, device_id: int, code: str) -> str | None:
    """Return the trip's share code, minting one the first time."""
    with connect() as con:
        row = con.execute(
            "SELECT share_code FROM trips WHERE id = ? AND device_id = ?",
            (trip_id, device_id),
        ).fetchone()
        if row is None:
            return None
        if row["share_code"]:
            return row["share_code"]
        con.execute("UPDATE trips SET share_code = ? WHERE id = ?", (code, trip_id))
        return code


async def share_trip(trip_id: int, device_id: int, code: str) -> str | None:
    return await asyncio.to_thread(_share_trip_blocking, trip_id, device_id, code)


def _by_share_blocking(code: str) -> dict | None:
    with connect() as con:
        row = con.execute(
            "SELECT * FROM trips WHERE share_code = ?", (code,)
        ).fetchone()
        return dict(row) if row else None


async def by_share(code: str) -> dict | None:
    return await asyncio.to_thread(_by_share_blocking, code)


def _already_watching_blocking(device_id: int, number: str, run_date: str,
                               from_slug: str, to_slug: str) -> int | None:
    with connect() as con:
        row = con.execute(
            """SELECT id FROM trips
                WHERE device_id = ? AND number = ? AND run_date = ?
                  AND from_slug = ? AND to_slug = ? AND active = 1""",
            (device_id, number, run_date, from_slug, to_slug),
        ).fetchone()
        return row["id"] if row else None


async def already_watching(device_id: int, number: str, run_date: str,
                           from_slug: str, to_slug: str) -> int | None:
    return await asyncio.to_thread(
        _already_watching_blocking, device_id, number, run_date, from_slug, to_slug)


class TripLimitReached(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__(f"limit of {limit} reached")
        self.limit = limit


def _count_active_blocking(device_id: int) -> int:
    with connect() as con:
        return con.execute(
            "SELECT COUNT(*) FROM trips WHERE device_id = ? AND active = 1",
            (device_id,),
        ).fetchone()[0]


async def count_active(device_id: int) -> int:
    return await asyncio.to_thread(_count_active_blocking, device_id)


def _add_trip_blocking(device_id: int, sub: dict, trip: dict) -> int:
    with connect() as con:
        # Take the write lock before counting, so two requests arriving
        # together cannot both see a free slot and both take it.
        con.execute("BEGIN IMMEDIATE")
        if sub:
            _upsert_sub(con, device_id, sub)
        active = con.execute(
            "SELECT COUNT(*) FROM trips WHERE device_id = ? AND active = 1", (device_id,)
        ).fetchone()[0]
        if active >= MAX_ACTIVE:
            raise TripLimitReached(MAX_ACTIVE)
        cur = con.execute(
            """INSERT INTO trips (device_id, number, run_date, from_slug, from_name,
                                  to_slug, to_name, created_at,
                                  dep_planned, arr_planned, branch_code)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                device_id,
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


async def add_trip(device_id: int, sub: dict, trip: dict) -> int:
    return await asyncio.to_thread(_add_trip_blocking, device_id, sub, trip)


def _finished_at(trip: dict) -> datetime | None:
    """When a retired trip stopped being interesting."""
    return _iso(trip.get("last_event")) or _iso(trip.get("arr_planned"))


def _list_trips_blocking(device_id: int) -> list[dict]:
    with connect() as con:
        rows = [
            dict(r)
            for r in con.execute(
                "SELECT * FROM trips WHERE device_id = ? ORDER BY id DESC",
                (device_id,),
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


async def list_trips(device_id: int) -> list[dict]:
    return await asyncio.to_thread(_list_trips_blocking, device_id)


def _delete_trip_blocking(trip_id: int, device_id: int) -> bool:
    with connect() as con:
        cur = con.execute(
            "DELETE FROM trips WHERE id = ? AND device_id = ?", (trip_id, device_id)
        )
        return cur.rowcount > 0


async def delete_trip(trip_id: int, device_id: int) -> bool:
    return await asyncio.to_thread(_delete_trip_blocking, trip_id, device_id)


def _active_blocking() -> list[dict]:
    with connect() as con:
        rows = con.execute(
            """SELECT t.*, s.endpoint, s.p256dh, s.auth
               FROM trips t
               JOIN devices d ON d.id = t.device_id AND d.revoked = 0
               JOIN push_subs s ON s.device_id = t.device_id
               WHERE t.active = 1"""
        ).fetchall()
        return [dict(r) for r in rows]


def _update_blocking(trip_id: int, fields: dict) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as con:
        con.execute(
            f"UPDATE trips SET {sets} WHERE id = ?", (*fields.values(), trip_id)
        )


def _drop_sub_blocking(endpoint: str) -> None:
    with connect() as con:
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
        late = f"{dep_delay:+d} min" if dep_delay else "la timp"
        events.append({
            "kind": "departed",
            "title": f"{label} a plecat din {trip['from_name']}",
            "body": f"A plecat la {_fmt(dep_dt)} ({late}). "
                    f"Sosire estimată în {trip['to_name']} la {_fmt(arr_dt)}.",
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
            direction = "a crescut" if arr_delay > known else "a scăzut"
            events.append({
                "kind": "delay",
                "title": f"{label}: întârzierea {direction} la {arr_delay} min",
                "body": f"Sosire estimată acum în {trip['to_name']} la "
                        f"{_fmt(arr_dt)} (era {known:+d} min, acum "
                        f"{arr_delay:+d} min).",
                "tag": f"trip-{trip['id']}-delay",
            })
            updates["last_delay"] = arr_delay

    # 3. arrival
    if not trip["arrived"] and arr_dt and now >= arr_dt:
        d = arr_delay or 0
        late = f"{d} min întârziere" if d > 0 else "la timp"
        events.append({
            "kind": "arrived",
            "title": f"{label} a sosit în {trip['to_name']}",
            "body": f"A sosit la {_fmt(arr_dt)} ({late}).",
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
    with connect() as con:
        row = con.execute(
            """SELECT t.*, s.endpoint, s.p256dh, s.auth
               FROM trips t
               LEFT JOIN push_subs s ON s.device_id = t.device_id
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
    with connect() as con:
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


async def watch_once(client, fetch=None) -> dict:
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
            # Shares the API's route cache when given one, so a train that a
            # user just looked up is not fetched twice.
            rt = await (fetch(number, when) if fetch
                        else R.fetch_route(client, number, when))
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
