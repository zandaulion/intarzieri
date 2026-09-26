"""Station-to-station search over CFR's published timetable.

InfoFer's own search by stations is ReCaptcha gated, but the timetable behind
it is open data: every passenger operator's year is published on data.gov.ro
as one XML file (`trenuri-2025-2026_sntfc.xml` and siblings). This module
downloads those files, reduces them to the stops a passenger can use, and
answers "which trains go from A to B on this date" locally -- no request to
CFR at all. Picking a result then goes through the ordinary itinerary lookup,
which is where live delays come from.

Reading the files, verified against the live site on 2026-09-26:

* A train is a list of `ElementTrasa`, one per segment. Element i starts at
  `CodStaOrigine`, leaves it at `OraP` and reaches the next station at `OraS`,
  in seconds after midnight. They are times of day, not elapsed time: a train
  running past midnight goes back to small numbers (occasionally a segment
  that crosses midnight overshoots 86400 instead), so day rollovers are
  counted here the way `route.py` counts them on the live site. The last
  element is a zero-length one whose origin is the terminus.
* `TipOprire` describes the element's origin station. `C` and `A` are stops
  the site lists; `N` is passing through and `T` a technical halt, neither of
  which it shows. The first station is marked `N` but is the train's origin.
  With that rule the stop lists match InfoFer's one for one, in order, which
  is how a result is mapped onto the live itinerary -- the station *names*
  do not match ("Ulmeni Hm." here is "Ulmeni" there).
* `CalendarTren` gives date ranges and a `Zile` bitmask: bit 0 is Monday
  through bit 6 Sunday. Public holidays are handled by the ranges themselves,
  which skip 25 December, Easter and the like. Higher bits are set on almost
  every train and mean something else; a calendar with none of the seven day
  bits belongs to a train InfoFer does not publish, so it never runs here.
* București Nord is published as two stations, `Gr.A` and `Gr.B`, its two
  platform groups. A passenger means both, so stations whose names differ
  only by such a suffix are searched as one.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import unicodedata
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

log = logging.getLogger("trains.timetable")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "timetable.db"
CKAN = "https://data.gov.ro/api/3/action/package_show"
# How often to ask data.gov.ro whether a file changed. Files are replaced a
# handful of times a year; asking daily costs seven small JSON requests.
REFRESH_SECONDS = int(os.getenv("TIMETABLE_REFRESH_SECONDS", str(24 * 3600)))

# data.gov.ro dataset -> the operator name shown next to a result.
DATASETS = {
    "mers-tren-sntfc-cfr-calatori-s-a": "CFR Călători",
    "regiocalatori": "Regio Călători",
    "mers-tren-interregional-calatori": "Interregional Călători",
    "mers-tren-transferoviar-calatori-s-r-l": "Transferoviar Călători",
    "astra_trans_carpatic": "Astra Trans Carpatic",
    "mers-tren-softrans-s-r-l": "Softrans",
    "mers-tren-2024-2025-ferotrafic-tfi": "Ferotrafic TFI",
}
# The newest file of the year is named for it; older years used other names.
SEASON_FILE = re.compile(r"trenuri-(\d{4})-(\d{4})_", re.I)
STOPS = {"C", "A"}

# " Gr.A", " Gr.B": platform groups of one station, not separate places.
PLATFORM_GROUP = re.compile(r"\s+Gr\.\s?[A-Z]$")

SCHEMA = """
CREATE TABLE stations (code INTEGER PRIMARY KEY, name TEXT NOT NULL,
                       norm TEXT NOT NULL, trains INTEGER NOT NULL,
                       grp INTEGER);
CREATE TABLE trains (id INTEGER PRIMARY KEY, number TEXT NOT NULL,
                     category TEXT, operator TEXT, stop_count INTEGER NOT NULL);
CREATE TABLE calendars (train_id INTEGER NOT NULL, start TEXT NOT NULL,
                        end TEXT NOT NULL, days INTEGER NOT NULL);
CREATE TABLE stops (train_id INTEGER NOT NULL, idx INTEGER NOT NULL,
                    station INTEGER NOT NULL, arr INTEGER, dep INTEGER);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""
INDEXES = """
CREATE INDEX stops_station ON stops (station, train_id);
CREATE INDEX stops_train ON stops (train_id, idx);
CREATE INDEX calendars_train ON calendars (train_id);
"""

# Suffixes CFR appends to station names: halts and their variants. Dropped
# when matching, never when displaying.
_SUFFIXES = {"h", "hm", "hc", "hcv", "tj", "gr"}


def display_name(name: str) -> str:
    """The file spells Romanian with cedillas (ş, ţ); the site uses commas."""
    return (name.replace("ş", "ș").replace("Ş", "Ș")
                .replace("ţ", "ț").replace("Ţ", "Ț").strip())


def normalise(name: str) -> str:
    """Lower case, no diacritics, no punctuation: what a search box types."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def match_key(name: str) -> str:
    """`normalise`, minus halt suffixes, for comparing against InfoFer names."""
    return " ".join(w for w in normalise(name).split() if w not in _SUFFIXES)


# ---------------------------------------------------------------- building

def _parse(path: Path, operator: str, db: sqlite3.Connection) -> tuple[int, str, str]:
    """Load one operator's file. Returns (trains, valid_from, valid_to)."""
    trains = 0
    valid = ("", "")
    names: dict[int, str] = {}
    for _, el in ET.iterparse(path, events=("end",)):
        if el.tag == "Mt":
            valid = (el.get("MtValabilDeLa", ""), el.get("MtValabilPinaLa", ""))
        if el.tag != "Tren":
            continue
        segments = sorted(el.iter("ElementTrasa"), key=lambda e: int(e.get("Secventa", 0)))
        calendars = [
            (c.get("DeLa", ""), c.get("PinaLa", ""), int(c.get("Zile") or 0))
            for c in el.iter("CalendarTren")
        ]
        calendars = [c for c in calendars if c[2] & 0x7F]
        if len(segments) < 2 or not calendars:
            el.clear()
            continue

        # Arrival at and departure from each segment's origin, as seconds
        # since midnight of the day the train starts.
        day, previous = 0, -1

        def elapsed(clock: str) -> int:
            nonlocal day, previous
            t = int(clock) % 86400
            while t + day * 86400 < previous:
                day += 1
            previous = t + day * 86400
            return previous

        last = len(segments) - 1
        times: list[tuple[int | None, int | None]] = []
        for i, seg in enumerate(segments):
            arr = elapsed(segments[i - 1].get("OraS")) if i > 0 else None
            dep = elapsed(seg.get("OraP")) if i < last else None
            times.append((arr, dep))

        stops: list[tuple[int, int | None, int | None]] = []
        for i, seg in enumerate(segments):
            code = int(seg.get("CodStaOrigine"))
            names.setdefault(code, seg.get("DenStaOrigine", ""))
            if not (i == 0 or i == last or seg.get("TipOprire") in STOPS):
                continue
            stops.append((code, *times[i]))
        if len(stops) < 2:
            el.clear()
            continue

        cur = db.execute(
            "INSERT INTO trains (number, category, operator, stop_count) VALUES (?,?,?,?)",
            (el.get("Numar"), el.get("CategorieTren"), operator, len(stops)),
        )
        tid = cur.lastrowid
        db.executemany("INSERT INTO calendars VALUES (?,?,?,?)",
                       [(tid, *c) for c in calendars])
        db.executemany("INSERT INTO stops VALUES (?,?,?,?,?)",
                       [(tid, i, *s) for i, s in enumerate(stops)])
        trains += 1
        el.clear()

    for code, name in names.items():
        db.execute(
            "INSERT OR IGNORE INTO stations (code, name, norm, trains) VALUES (?,?,?,0)",
            (code, display_name(name), normalise(name)),
        )
    return trains, *valid


def _group_platforms(db: sqlite3.Connection) -> None:
    """Point every station at the one it is searched as: itself, or for a
    platform group the busiest station sharing its base name, which also
    takes the base name for display."""
    db.execute("UPDATE stations SET grp = code")
    groups: dict[str, list[tuple[int, str, int]]] = {}
    for code, name, trains in db.execute("SELECT code, name, trains FROM stations"):
        if PLATFORM_GROUP.search(name):
            base = PLATFORM_GROUP.sub("", name)
            groups.setdefault(base, []).append((code, name, trains))
    for base, members in groups.items():
        lead = max(members, key=lambda m: m[2])[0]
        for code, _, _ in members:
            db.execute("UPDATE stations SET grp = ? WHERE code = ?", (lead, code))
        db.execute("UPDATE stations SET name = ?, norm = ? WHERE code = ?",
                   (base, normalise(base), lead))


def build(files: list[tuple[Path, str]], target: Path = DB_PATH) -> dict:
    """Write a fresh database from downloaded files, then swap it in whole.

    Built beside the live one and renamed over it, so a search never sees a
    half-written index and a failed build leaves the previous one serving.
    """
    tmp = target.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    try:
        db.executescript(SCHEMA)
        total, seasons = 0, []
        for path, operator in files:
            n, start, end = _parse(path, operator, db)
            total += n
            seasons.append((start, end))
            log.info("timetable: %s -> %d trains (%s..%s)", path.name, n, start, end)
        # Stations nothing stops at (junctions, passing points) would only
        # clutter the suggestions.
        db.execute("""UPDATE stations SET trains =
                      (SELECT COUNT(DISTINCT train_id) FROM stops WHERE station = code)""")
        db.execute("DELETE FROM stations WHERE trains = 0")
        _group_platforms(db)
        db.executescript(INDEXES)
        valid_to = max((s[1] for s in seasons if s[1]), default="")
        db.executemany("INSERT INTO meta VALUES (?,?)", [
            ("built_at", datetime.now().astimezone().isoformat(timespec="seconds")),
            ("trains", str(total)),
            ("valid_to", valid_to),
        ])
        db.commit()
    finally:
        db.close()
    os.replace(tmp, target)
    return {"trains": total, "valid_to": valid_to}


# ---------------------------------------------------------------- fetching

async def _resources(client: httpx.AsyncClient) -> list[dict]:
    """The files to use: per operator, every current-format file, newest two.

    Two, because a new year is published in early December while the old one
    still runs until the timetable change mid-month. Calendars carry absolute
    dates, so holding both years answers either side of the change correctly.
    """
    chosen = []
    for dataset, operator in DATASETS.items():
        r = await client.get(CKAN, params={"id": dataset})
        r.raise_for_status()
        files = [
            res for res in r.json()["result"]["resources"]
            if SEASON_FILE.search(res.get("url", ""))
        ]
        files.sort(key=lambda res: SEASON_FILE.search(res["url"]).group(1), reverse=True)
        for res in files[:2]:
            chosen.append({
                "id": res["id"], "url": res["url"], "operator": operator,
                "stamp": res.get("last_modified") or res.get("created") or "",
            })
    return chosen


def _stored_sources() -> str:
    if not DB_PATH.exists():
        return ""
    try:
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as db:
            row = db.execute("SELECT value FROM meta WHERE key='sources'").fetchone()
            return row[0] if row else ""
    except sqlite3.Error:
        return ""


async def refresh(client: httpx.AsyncClient, force: bool = False) -> bool:
    """Rebuild if data.gov.ro now lists different files. True if it rebuilt."""
    resources = await _resources(client)
    signature = ";".join(sorted(f"{r['id']}@{r['stamp']}" for r in resources))
    if not force and signature == _stored_sources():
        return False

    work = DATA_DIR / "timetable-src"
    work.mkdir(parents=True, exist_ok=True)
    files = []
    for res in resources:
        path = work / f"{res['id']}.xml"
        async with client.stream("GET", res["url"]) as r:
            r.raise_for_status()
            with open(path, "wb") as fh:
                async for chunk in r.aiter_bytes():
                    fh.write(chunk)
        files.append((path, res["operator"]))

    summary = await asyncio.to_thread(build, files)
    with sqlite3.connect(DB_PATH) as db:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('sources', ?)", (signature,))
    for path, _ in files:
        path.unlink(missing_ok=True)
    log.info("timetable rebuilt: %s", summary)
    return True


async def keep_fresh(user_agent: str) -> None:
    """Background task: build on first start, then check once a day."""
    async with httpx.AsyncClient(
        timeout=120, follow_redirects=True, headers={"User-Agent": user_agent}
    ) as client:
        while True:
            try:
                await refresh(client)
            except Exception as exc:  # noqa: BLE001 - keep serving the old one
                log.warning("timetable refresh failed: %r", exc)
            await asyncio.sleep(REFRESH_SECONDS)


# ---------------------------------------------------------------- querying

class NotReady(Exception):
    """The first build has not finished yet."""


def _db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise NotReady
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def info() -> dict | None:
    try:
        with _db() as db:
            return {r["key"]: r["value"] for r in db.execute("SELECT * FROM meta")
                    if r["key"] != "sources"}
    except (NotReady, sqlite3.Error):
        return None


def stations(query: str, limit: int = 10) -> list[dict]:
    """Suggestions for a search box: names starting with the text first,
    then names with a word starting with it, busiest stations first."""
    q = normalise(query)
    if not q:
        return []
    with _db() as db:
        rows = db.execute(
            """SELECT code, name, trains,
                      CASE WHEN norm LIKE :p THEN 0 ELSE 1 END AS rank
               FROM stations
               WHERE code = grp AND (norm LIKE :p OR norm LIKE :w)
               ORDER BY rank, trains DESC, name
               LIMIT :n""",
            {"p": f"{q}%", "w": f"% {q}%", "n": limit},
        ).fetchall()
    return [{"code": r["code"], "name": r["name"]} for r in rows]


def station(code: int) -> dict | None:
    with _db() as db:
        r = db.execute("""SELECT lead.code, lead.name FROM stations s
                          JOIN stations lead ON lead.code = s.grp
                          WHERE s.code = ?""", (code,)).fetchone()
    return dict(r) if r else None


def _runs_on(calendars: list[sqlite3.Row], day: date) -> bool:
    stamp = day.strftime("%Y%m%d")
    bit = 1 << day.weekday()
    return any(c["start"] <= stamp <= c["end"] and c["days"] & bit for c in calendars)


def _clock(seconds: int) -> str:
    seconds %= 86400
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"


def search(origin: int, destination: int, day: date) -> list[dict]:
    """Trains that leave `origin` on `day` and later stop at `destination`.

    `day` is the date the passenger boards. A train that started the evening
    before and reaches `origin` after midnight is found through its own start
    date, the day before, which is also the run date the itinerary lookup
    needs.
    """
    with _db() as db:
        legs = db.execute(
            """SELECT t.id, t.number, t.category, t.operator, t.stop_count,
                      a.idx AS from_idx, a.dep, b.idx AS to_idx, b.arr
               FROM stops a
               JOIN stops b ON b.train_id = a.train_id AND b.idx > a.idx
               JOIN trains t ON t.id = a.train_id
               WHERE a.station IN (SELECT code FROM stations WHERE grp =
                                     (SELECT grp FROM stations WHERE code = ?))
                 AND b.station IN (SELECT code FROM stations WHERE grp =
                                     (SELECT grp FROM stations WHERE code = ?))
                 AND a.dep IS NOT NULL AND b.arr IS NOT NULL""",
            (origin, destination),
        ).fetchall()
        found = []
        for leg in legs:
            offset = leg["dep"] // 86400        # days after the train's start
            run_date = day - timedelta(days=offset)
            cals = db.execute("SELECT * FROM calendars WHERE train_id=?",
                              (leg["id"],)).fetchall()
            if not _runs_on(cals, run_date):
                continue
            ends = db.execute(
                """SELECT lead.name FROM stops p
                   JOIN stations s ON s.code = p.station
                   JOIN stations lead ON lead.code = s.grp
                   WHERE p.train_id = ? AND p.idx IN (0, ?) ORDER BY p.idx""",
                (leg["id"], leg["stop_count"] - 1),
            ).fetchall()
            found.append({
                "number": leg["number"],
                "category": leg["category"],
                "operator": leg["operator"],
                "run_date": run_date.isoformat(),
                "departs": _clock(leg["dep"]),
                "arrives": _clock(leg["arr"]),
                # Whole days between boarding and arriving: 1 for overnight.
                "arrives_day_offset": leg["arr"] // 86400 - offset,
                "duration_min": (leg["arr"] - leg["dep"]) // 60,
                "from_index": leg["from_idx"],
                "to_index": leg["to_idx"],
                "stop_count": leg["stop_count"],
                "origin": ends[0]["name"] if ends else None,
                "terminus": ends[-1]["name"] if ends else None,
                "_sort": leg["dep"] % 86400,
            })
    # Two files can publish the same train twice across a year change; one
    # line per train and time is enough.
    unique = {(f["number"], f["departs"]): f for f in found}
    result = sorted(unique.values(), key=lambda f: f["_sort"])
    for f in result:
        del f["_sort"]
    return result
