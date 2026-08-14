"""Parse the live train map that mersultrenurilor.infofer.ro renders for Leaflet.

The upstream endpoint returns a JavaScript partial, not JSON: one block per
train, each building a marker. We extract the fields we need per block.

This is inherently coupled to their page structure and will break if they
change it -- parse_map() is written to skip malformed blocks rather than fail
the whole response, so one upstream change degrades coverage instead of
taking the service down.
"""
from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass, asdict

MAP_URL = "https://mersultrenurilor.infofer.ro/ro-RO/Trains/LoadMapPartial"
REFERER = "https://mersultrenurilor.infofer.ro/ro-RO/TrainsMap"

# Each train block starts with its SVG icon definition.
_BLOCK = re.compile(r"var icon\s*=\s*L\.svgIcon\(")
# The popup class is authoritative; the icon fill colour varies by category.
_CATEGORY = re.compile(r"span-train-category-([a-z]+)")
_CATEGORY_FALLBACK = re.compile(r'font-weight="bold"[^>]*>([A-Za-zĂÂÎȘŢȚăâîșşţț]{1,4})</tspan>')
_NUMBER = re.compile(r'<tspan x="20" dy="1\.2em"[^>]*>\s*([0-9]{1,6})\s*</tspan>')
# Two position flavours: a real GPS fix (lastGpsPosition*) or one computed
# from the timetable (theoreticalGpsPosition*).
_LAT = re.compile(r"(last|theoretical)GpsPositionLatitude\s*=\s*(-?\d+\.?\d*)")
_LON = re.compile(r"(last|theoretical)GpsPositionLongitude\s*=\s*(-?\d+\.?\d*)")
_PASSED = re.compile(r"passedTime:\s*(\d+)")
_DELAY = re.compile(r"<b>\s*(\d+)\s*min\s*</b>\s*întârziere", re.I)
_ONTIME = re.compile(r"fără\s+întârziere", re.I)
# "RAPORTAT de personalul CFR la 16:32"  (an actual staff report)
_REPORTED = re.compile(r"RAPORTAT[^<]*?la\s+(\d{1,2}:\d{2})", re.I)
# "Poziție ESTIMATĂ pe baza raportării CFR de la 16:33"  (extrapolated)
_ESTIMATED = re.compile(r"ESTIMAT[ĂA][^<]*?de la\s+(\d{1,2}:\d{2})", re.I)
_RUNNING_NO = re.compile(r"TrainRunningNumber=(\d+)")


@dataclass
class Train:
    number: str
    category: str
    delay_min: int
    on_time: bool
    lat: float
    lon: float
    reported_at: str | None      # HH:MM the delay was actually measured
    report_kind: str             # "reported" | "estimated" | "unknown"
    position_source: str         # "gps" (real fix) | "theoretical" (computed)
    minutes_since_report: int | None

    def dict(self) -> dict:
        return asdict(self)


def haversine_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def parse_map(raw: str) -> list[Train]:
    """Split the JS partial into per-train blocks and extract each one."""
    starts = [m.start() for m in _BLOCK.finditer(raw)]
    trains: list[Train] = []
    for i, s in enumerate(starts):
        block = raw[s: starts[i + 1] if i + 1 < len(starts) else len(raw)]
        text = html.unescape(block)

        num = _NUMBER.search(text)
        if not num:
            m = _RUNNING_NO.search(text)
            if not m:
                continue
            number = m.group(1).lstrip("0") or m.group(1)
        else:
            number = num.group(1).lstrip("0") or num.group(1)

        lat, lon = _LAT.search(text), _LON.search(text)
        if not (lat and lon):
            continue
        source = "gps" if lat.group(1) == "last" else "theoretical"

        d = _DELAY.search(text)
        delay = int(d.group(1)) if d else 0
        on_time = bool(_ONTIME.search(text)) and not d

        rep, est = _REPORTED.search(text), _ESTIMATED.search(text)
        if rep:
            when, kind = rep.group(1), "reported"
        elif est:
            when, kind = est.group(1), "estimated"
        else:
            when, kind = None, "unknown"

        passed = _PASSED.search(text)
        cat = _CATEGORY.search(text) or _CATEGORY_FALLBACK.search(text)

        trains.append(Train(
            number=number,
            category=(cat.group(1).upper() if cat else "?"),
            delay_min=delay,
            on_time=on_time,
            lat=float(lat.group(2)),
            lon=float(lon.group(2)),
            reported_at=when,
            report_kind=kind,
            position_source=source,
            minutes_since_report=int(passed.group(1)) if passed else None,
        ))
    return trains
