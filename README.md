# Train Watch

Watch one Romanian train between two stations and get a push notification when
it departs, when its delay changes, and when it arrives.

Live at `https://<public-host>` (Tailscale Funnel → Caddy → rootless podman).

## Where the data comes from

There is no public CFR API. The old `appiris.infofer.ro` endpoint is dead
(NXDOMAIN) and the search flow on `mersultrenurilor.infofer.ro` is ReCaptcha
gated. Two endpoints on that site are not:

| Endpoint | What it gives | Used for |
|---|---|---|
| `GET /ro-RO/Trains/LoadMapPartial` | ~1.7 MB of Leaflet markers: every running train, position, delay | live map / `/api/trains` |
| `POST /ro-RO/Trains/TrainsResult` | one train's full itinerary: ordered stations, scheduled arr/dep, per-station delay | everything else |

The POST needs an antiforgery token and a `ConfirmationKey`, both of which the
`GET /ro-RO/Train/{number}` page hands out; the `ReCaptcha` field is accepted
empty on the first attempt. So one itinerary costs two requests.

An identifying `User-Agent` is sent and works — no browser spoofing needed.

### Branches

A train may be published as several *branches*: alternative descriptions of
the same run. IR 1996 appears as both `Constanța–Craiova` (12 stops) and
`Mangalia–Craiova` (21 stops). They are separate `div-stations-branch-*`
sections and **must** be parsed separately — concatenating them produces
duplicate stations and a timeline that jumps backwards. The one InfoFer shows
by default is the one without `d-none`.

## How events are decided

InfoFer publishes no "has departed" flag, so a station counts as passed once
`scheduled_time + currently_reported_delay` is in the past — the same
arithmetic a passenger on the platform does.

- **departed** — once past the expected departure from the chosen origin.
- **delay** — when the delay at the chosen destination moves by at least
  `DELAY_THRESHOLD` (default 5 min) *since the last notification sent*, not
  since the last poll, so small drift never accumulates into spam.
- **arrived** — once past the expected arrival at the chosen destination;
  retires the trip.

A retired trip is never polled again. It stays visible in the user's list for
`LIST_KEEP_HOURS` (12) so this morning's train is still there, and is deleted
outright `PURGE_AFTER_HOURS` (48) after it finished so the table cannot grow
without bound. Only retired trips are ever purged, so an overdue train that is
still being watched is safe.

Two retirement paths are distinguishable without an extra column:
`arrived = 1` means an arrival was actually detected; `active = 0` with
`arrived = 0` means the 6-hour fallback fired because the train stopped being
published. The UI shows the second as *no longer tracked* rather than
pretending it is still en route.

Subscribing to a train that already left does **not** fire a departure
notification: `trips.prime()` adopts the current state silently.

Values InfoFer marks with `*` are its own projection between staff reports
rather than a report; the UI tags these `est`.

## Load on the source

- The live map is fetched **lazily** — only when a client asks and the cached
  copy is older than `POLL_SECONDS`. An idle server makes no requests.
- Itineraries are fetched only for trains somebody is watching, only inside
  that train's journey window (`LEAD_MINUTES` before scheduled departure until
  arrival + delay + `MAX_OVERDUE_HOURS`), and **grouped by train** — ten people
  watching the same train cost one fetch, not ten.

## Layout

```
backend/
  iris.py     live-map parser (positions, delays, nearest station)
  route.py    itinerary parser: branches, stops, day rollover, delays
  trips.py    SQLite store + event detection + watcher loop
  push.py     VAPID keypair + Web Push sending
  app.py      FastAPI: /api/route, /api/trips, /api/vapid, /api/trains
web/          the PWA (no build step, no framework)
quadlet/      systemd unit for rootless podman
```

State lives in the named volume `train-api-data` (`/data`): the VAPID keypair
and `trips.db`. **The keypair must survive rebuilds** — regenerating it
silently invalidates every browser subscription already issued.

## Deploying

```sh
./deploy.sh          # web assets only
./deploy.sh --api    # also rebuild the image and restart the unit
```

Assets are stamped with a content hash so the service worker and the
`?v=` query strings change together.

## Caveats

- **The parsers are coupled to InfoFer's HTML** and will break when it changes.
  Mitigated, not solved: malformed blocks are skipped individually, the last
  good map snapshot keeps serving, and `/api/health` exposes `stale`,
  `consecutive_failures` and the last watch pass.
- **Overnight trains** wrap past midnight; day offsets are inferred from times
  moving backwards, tracked per arrival/departure rather than per station
  (a train can arrive 23:51 and depart 00:02).
- **A push endpoint is a bearer capability.** Anyone holding one can list or
  delete its trips; there is no other authentication and no rate limiting.
- **iOS** only exposes Web Push to PWAs installed to the Home Screen.
- Delays are reported by station staff, so they are as accurate — and as
  granular — as those reports.
