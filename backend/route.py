"""Per-train itinerary from the public "Trenul meu" page.

The page itself is a shell; the station list arrives from a POST to
/ro-RO/Trains/TrainsResult. That POST needs an antiforgery token and a
ConfirmationKey which the GET page hands out for free -- the ReCaptcha field
is left empty on the first attempt and the server accepts it. So the flow is
GET (collect tokens + cookies) then POST (get stations), two requests per
train per poll.
"""
from __future__ import annotations

import html as H
import logging
import re
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("trains.route")

BASE = "https://mersultrenurilor.infofer.ro"
TRAIN_URL = BASE + "/ro-RO/Train/{number}"
RESULT_URL = BASE + "/ro-RO/Trains/TrainsResult"
RO = ZoneInfo("Europe/Bucharest")

_FORM = re.compile(r'(?s)<form id="form-search".*?</form>')
_INPUT = re.compile(r"<input[^>]*>")
_NAME = re.compile(r'\bname="([^"]+)"')
_VALUE = re.compile(r'\bvalue="([^"]*)"')

# A train can be published as several "branches" -- alternative descriptions
# of its run, e.g. Constanța-Craiova and Mangalia-Craiova for the same train.
# They must be parsed separately or their stations interleave into nonsense.
_BRANCH = re.compile(
    r'(?s)<div id="div-stations-branch-(\d+)"([^>]*)>'
    r'(.*?)(?=<div id="div-stations-branch-|\Z)'
)
_BRANCH_NAME = re.compile(r"(?s)<h4>\s*Parcurs tren\s*(.*?)\s*</h4>")
_POSITION = re.compile(r"(?s)Conform itinerariului.*?(?:\.\s*<|</p>)")
_LI = re.compile(r'(?s)<li class="list-group-item">(.*?)</li>')
_STATION = re.compile(r'<a href="/ro-RO/Statie/([^"?]+)\?[^"]*">\s*([^<]+?)\s*</a>')
_KM = re.compile(r"km\s+(\d+)")
_STOP = re.compile(r"(\d+)\s+min\s+oprire")
_LINE = re.compile(r"linia\s+(\S+)")
# A time cell and the delay note that follows it. `text-right` marks the
# departure side of the row; its absence marks the arrival side.
_TIMECELL = re.compile(
    r'(?s)<div class="text-1-3rem([^"]*)">\s*(\d{1,2}:\d{2})\s*</div>'
    r'(?:\s*<div class="text-0-8rem([^"]*)">\s*([^<]*?)\s*</div>)?'
)
_DELAY = re.compile(r"([+-]?\d+)\s*min")
_ONTIME = re.compile(r"la\s+timp", re.I)
_SUMMARY = re.compile(
    r"(?s)<i class=\"fas fa-stopwatch\"></i>.*?(?:<span[^>]*>\s*(.*?)\s*</span>)"
)
_REPORTED_AT = re.compile(r"Raportat la\s+(\d{1,2}:\d{2})")
_TITLE = re.compile(r"span-train-category-([a-z-]+)\s*\">\s*([A-Za-z-]+)\s*</span>")


@dataclass
class Stop:
    slug: str
    name: str
    km: int | None
    line: str | None
    stop_minutes: int | None
    arr_scheduled: str | None      # "HH:MM" local
    arr_delay: int | None          # minutes, + = late
    arr_estimated: bool            # value is InfoFer's estimate, not a report
    dep_scheduled: str | None
    dep_delay: int | None
    dep_estimated: bool
    # Days past the route's start date. Tracked per time, not per stop: a
    # train can arrive at 23:51 and depart at 00:02 -- different days.
    arr_day_offset: int = 0
    dep_day_offset: int = 0

    def dict(self) -> dict:
        return asdict(self)


@dataclass
class Branch:
    code: str
    name: str                      # e.g. "Constanța–Craiova"
    is_default: bool               # the one InfoFer shows without unfolding
    stops: list[Stop]
    summary_delay: int | None
    reported_at: str | None
    position_note: str | None      # "trenul se află între stațiile X - Y"

    def dict(self) -> dict:
        d = asdict(self)
        d["stops"] = [s.dict() for s in self.stops]
        return d


@dataclass
class Route:
    number: str
    category: str | None
    date: str                      # "DD.MM.YYYY" as InfoFer sees it
    branches: list[Branch]

    @property
    def default(self) -> Branch:
        for b in self.branches:
            if b.is_default:
                return b
        return self.branches[0]

    @property
    def stops(self) -> list[Stop]:
        return self.default.stops

    @property
    def summary_delay(self) -> int | None:
        return self.default.summary_delay

    @property
    def reported_at(self) -> str | None:
        return self.default.reported_at

    def branch_for(self, from_slug: str, to_slug: str) -> Branch | None:
        """The branch that actually contains this leg, in the right order.

        Prefers the default branch so an unambiguous leg keeps InfoFer's own
        view of the train.
        """
        candidates = []
        for b in self.branches:
            idx = {st.slug: i for i, st in enumerate(b.stops)}
            if from_slug in idx and to_slug in idx and idx[from_slug] < idx[to_slug]:
                candidates.append(b)
        if not candidates:
            return None
        for b in candidates:
            if b.is_default:
                return b
        return candidates[0]

    def dict(self) -> dict:
        return {
            "number": self.number,
            "category": self.category,
            "date": self.date,
            "branches": [b.dict() for b in self.branches],
        }


def _clean(fragment: str) -> str:
    """Tag soup -> one line of readable text."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip(" .<")


def _parse_delay(text: str) -> tuple[int | None, bool]:
    """-> (minutes, is_estimated). '' means no information at all."""
    if not text:
        return None, False
    estimated = "*" in text
    if _ONTIME.search(text):
        return 0, estimated
    m = _DELAY.search(text)
    if not m:
        return None, estimated
    return int(m.group(1)), estimated


def _parse_stops(body: str) -> list[Stop]:
    stops: list[Stop] = []
    for block in _LI.findall(body):
        st = _STATION.search(block)
        if not st:
            continue
        km = _KM.search(block)
        line = _LINE.search(block)
        stop_m = _STOP.search(block)

        arr = dep = None
        for cls, hhmm, dcls, dtext in _TIMECELL.findall(block):
            is_dep = "text-right" in cls or "text-right" in dcls
            delay, est = _parse_delay(dtext)
            if is_dep:
                dep = (hhmm, delay, est)
            else:
                arr = (hhmm, delay, est)

        stops.append(
            Stop(
                slug=st.group(1),
                name=H.unescape(st.group(2)),
                km=int(km.group(1)) if km else None,
                line=line.group(1) if line else None,
                stop_minutes=int(stop_m.group(1)) if stop_m else None,
                arr_scheduled=arr[0] if arr else None,
                arr_delay=arr[1] if arr else None,
                arr_estimated=bool(arr[2]) if arr else False,
                dep_scheduled=dep[0] if dep else None,
                dep_delay=dep[1] if dep else None,
                dep_estimated=bool(dep[2]) if dep else False,
            )
        )
    _assign_day_offsets(stops)
    return stops


def _assign_day_offsets(stops: list[Stop]) -> None:
    """Overnight trains wrap past midnight (23:39 -> 00:02). Scheduled times
    only carry HH:MM, so a time that moves backwards means a new day."""
    offset = 0
    prev = None
    for st in stops:
        for attr in ("arr", "dep"):
            hhmm = getattr(st, f"{attr}_scheduled")
            if not hhmm:
                continue
            h, m = (int(x) for x in hhmm.split(":"))
            mins = h * 60 + m
            if prev is not None and mins < prev - 180:
                offset += 1
            prev = mins
            setattr(st, f"{attr}_day_offset", offset)


def parse_route(number: str, when: str, body: str) -> Route:
    body = H.unescape(body)

    cat = None
    t = _TITLE.search(body)
    if t:
        cat = t.group(2).strip()

    branches: list[Branch] = []
    for code, attrs, chunk in _BRANCH.findall(body):
        stops = _parse_stops(chunk)
        if not stops:
            continue
        name = ""
        n = _BRANCH_NAME.search(chunk)
        if n:
            name = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", n.group(1))).strip()
        delay = None
        sm = _SUMMARY.search(chunk)
        if sm:
            delay, _ = _parse_delay(sm.group(1) or "")
        rep = _REPORTED_AT.search(chunk)
        pos = _POSITION.search(chunk)
        branches.append(
            Branch(
                code=code,
                name=name or f"{stops[0].name}–{stops[-1].name}",
                # InfoFer collapses the alternates with d-none.
                is_default="d-none" not in attrs,
                stops=stops,
                summary_delay=delay,
                reported_at=rep.group(1) if rep else None,
                position_note=_clean(pos.group(0)) if pos else None,
            )
        )

    if not branches:
        # No branch wrappers at all -- older/simpler pages list stops directly.
        stops = _parse_stops(body)
        if stops:
            delay = None
            sm = _SUMMARY.search(body)
            if sm:
                delay, _ = _parse_delay(sm.group(1) or "")
            rep = _REPORTED_AT.search(body)
            branches.append(
                Branch(
                    code="0",
                    name=f"{stops[0].name}–{stops[-1].name}",
                    is_default=True,
                    stops=stops,
                    summary_delay=delay,
                    reported_at=rep.group(1) if rep else None,
                    position_note=None,
                )
            )

    return Route(number=number, category=cat, date=when, branches=branches)


async def fetch_route(
    client: httpx.AsyncClient, number: str, when: date | None = None
) -> Route:
    """Two requests: GET for tokens, POST for the station list."""
    when = when or datetime.now(RO).date()
    ro_date = when.strftime("%d.%m.%Y") + " 0:00:00"

    page = await client.get(TRAIN_URL.format(number=number))
    page.raise_for_status()

    form = _FORM.search(page.text)
    if not form:
        raise ValueError("search form not found -- page structure changed")

    fields: dict[str, str] = {}
    for tag in _INPUT.findall(form.group(0)):
        n = _NAME.search(tag)
        if not n:
            continue
        v = _VALUE.search(tag)
        fields[n.group(1)] = H.unescape(v.group(1)) if v else ""

    fields["Date"] = ro_date
    fields["TrainRunningNumber"] = number
    fields["IsSearchWanted"] = "True"

    res = await client.post(
        RESULT_URL,
        content=urlencode(fields),
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": TRAIN_URL.format(number=number),
        },
    )
    res.raise_for_status()
    text = res.text
    if text.strip() in ("ReCaptchaFailed", "ServiceTemporarilyUnavailable"):
        raise ValueError(f"upstream refused: {text.strip()}")

    route = parse_route(number, when.strftime("%d.%m.%Y"), text)
    if not route.branches:
        raise ValueError("no stations parsed -- train may not run on this date")
    return route


def local_dt(day: date, hhmm: str, day_offset: int = 0) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(day, datetime.min.time(), tzinfo=RO) + timedelta(
        days=day_offset, hours=h, minutes=m
    )


def actual_dt(
    day: date, hhmm: str | None, delay: int | None, day_offset: int = 0
) -> datetime | None:
    """Scheduled time shifted by the reported delay -> expected real time."""
    if not hhmm:
        return None
    return local_dt(day, hhmm, day_offset) + timedelta(minutes=delay or 0)


def parse_ro_date(s: str) -> date:
    d, m, y = (int(x) for x in s.split(".")[:3])
    return date(y, m, d)
