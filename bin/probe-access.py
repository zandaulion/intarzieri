#!/usr/bin/env python3
"""Characterise how CFR answers *this* machine.

probe.sh answers "is it up", which is the right question when the site falls
over. It is the wrong question for the one being asked here: whether the same
site treats two connections differently. A geo-block, a WAF challenge or a
ReCaptcha gate all return a perfectly healthy HTTP status, and probe.sh counts
every one of them as reachable.

So this walks the flow the app actually depends on and reports what came back
at each step, in a form two machines can be diffed against each other:

    python3 bin/probe-access.py            # pick a running train itself
    python3 bin/probe-access.py 1592       # or name one

Standard library only, so it runs on whatever is to hand at the other end.
"""

import gzip
import json
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from urllib.parse import urlencode

HOST = "mersultrenurilor.infofer.ro"
BASE = f"https://{HOST}"
UA = "a1-train-tracker/1.0 (personal self-hosted dashboard; low-rate polling)"

# Same shape the backend pulls the antiforgery fields out of.
_FORM = re.compile(r'(?s)<form id="form-search".*?</form>')
_INPUT = re.compile(r"<input[^>]*>")
_NAME = re.compile(r'\bname="([^"]+)"')
_VALUE = re.compile(r'\bvalue="([^"]*)"')

jar = CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def fetch(url, data=None, headers=None, timeout=20):
    """Returns (status, body, headers, seconds). A 4xx/5xx is an answer, not an error."""
    req = urllib.request.Request(url, data=data)
    req.add_header("User-Agent", UA)
    req.add_header("Accept-Encoding", "gzip")
    for k, v in (headers or {}).items():
        req.add_header(k, v)

    started = time.monotonic()
    try:
        res = opener.open(req, timeout=timeout)
        status, raw, hdrs = res.status, res.read(), res.headers
    except urllib.error.HTTPError as e:
        status, raw, hdrs = e.code, e.read(), e.headers
    except Exception as e:                                  # DNS, TCP, TLS, timeout
        return None, f"{type(e).__name__}: {e}", {}, time.monotonic() - started

    if hdrs.get("Content-Encoding") == "gzip":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    return status, raw.decode("utf-8", "replace"), hdrs, time.monotonic() - started


def line(label, value):
    print(f"  {label:<22} {value}")


def looks_challenged(body):
    """The failures that arrive wearing a 200.

    A ReCaptcha *field* on the search form is not one of them: it is always
    there and the site accepts it empty, which is the whole reason this app
    works. Only a refusal counts -- the site saying ReCaptchaFailed when the
    itinerary was asked for, or an interstitial standing in front of the page.
    """
    # The refusals are the entire body, never a mention inside a page -- the
    # search page ships JavaScript that names them, and matching on a substring
    # reported every healthy page as a refusal.
    stripped = body.strip()
    if stripped in ("ReCaptchaFailed", "ServiceTemporarilyUnavailable"):
        return f"REFUSED: {stripped}"

    low = body.lower()
    for needle, what in (
        ("cf-browser-verification", "Cloudflare challenge"),
        ("just a moment", "Cloudflare interstitial"),
        ("checking your browser", "browser check"),
        ("access denied", "access denied page"),
        ("<title>403", "403 page"),
    ):
        if needle in low:
            return what
    return None


print(f"probing {HOST}")
print()

# ---------------------------------------------------------------- who we are
print("network")
try:
    ips = sorted({r[4][0] for r in socket.getaddrinfo(HOST, 443)})
    line("resolves to", ", ".join(ips))
except Exception as e:
    line("resolves to", f"FAILED: {e}")
    ips = []

try:
    st, body, _, _ = fetch("https://api.ipify.org", timeout=10)
    line("our egress ip", body.strip() if st == 200 else f"unknown ({st})")
except Exception:
    line("our egress ip", "unknown")

if ips:
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((ips[0], 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=HOST) as tls:
                cert = tls.getpeercert()
                issuer = dict(x[0] for x in cert["issuer"]).get("organizationName", "?")
                line("tls", f"{tls.version()}  issuer={issuer}  expires={cert['notAfter']}")
    except Exception as e:
        line("tls", f"FAILED: {e}")

# ------------------------------------------------------------------ the site
print()
print("homepage")
st, body, hdrs, secs = fetch(BASE + "/ro-RO/")
line("status", f"{st}   {len(body)} bytes   {secs:.2f}s")
line("server", hdrs.get("Server", "(not sent)") if hdrs else "-")
if hdrs:
    edge = [k for k in ("CF-RAY", "X-Cache", "Via", "X-Served-By") if hdrs.get(k)]
    line("cdn/edge", ", ".join(f"{k}={hdrs[k]}" for k in edge) if edge else "none (direct)")
flag = looks_challenged(body) if isinstance(body, str) else None
line("challenge", flag or "none")

# ------------------------------------------------------- the live map (1.7 MB)
print()
print("map endpoint  /ro-RO/Trains/LoadMapPartial")
st, body, hdrs, secs = fetch(BASE + "/ro-RO/Trains/LoadMapPartial")
line("status", f"{st}   {len(body)} bytes   {secs:.2f}s")
running = re.findall(r"Number=(\d+)", body) if st == 200 else []
line("train numbers seen", len(set(running)))
flag = looks_challenged(body) if st == 200 else None
line("challenge", flag or "none")

# ------------------------------------------------------------- one itinerary
number = sys.argv[1] if len(sys.argv) > 1 else (sorted(set(running))[0] if running else "1592")
print()
print(f"itinerary flow  train {number}")

st, page, hdrs, secs = fetch(BASE + f"/ro-RO/Train/{number}")
line("GET Train/N", f"{st}   {len(page)} bytes   {secs:.2f}s")
flag = looks_challenged(page) if st == 200 else None
line("challenge", flag or "none")

form = _FORM.search(page or "")
fields = {}
if form:
    for tag in _INPUT.findall(form.group(0)):
        n = _NAME.search(tag)
        if n:
            v = _VALUE.search(tag)
            fields[n.group(1)] = v.group(1) if v else ""
line("antiforgery token", "yes" if any("RequestVerificationToken" in k for k in fields) else "NO")
line("ConfirmationKey", "yes" if "ConfirmationKey" in fields else "NO")

if fields:
    fields["IsSearchWanted"] = "True"
    st, res, hdrs, secs = fetch(
        BASE + "/ro-RO/Trains/TrainsResult",
        data=urlencode(fields).encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": BASE + f"/ro-RO/Train/{number}",
        },
    )
    line("POST TrainsResult", f"{st}   {len(res)} bytes   {secs:.2f}s")
    stripped = res.strip()
    if stripped in ("ReCaptchaFailed", "ServiceTemporarilyUnavailable"):
        line("upstream said", f"REFUSED: {stripped}")
    else:
        line("stations parsed", len(re.findall(r"div-stations-branch-", res)))
        line("challenge", looks_challenged(res) or "none")

# -------------------------------------------------- does it tire of us asking
print()
print("five in a row, half a second apart")
codes = []
for i in range(5):
    st, b, _, secs = fetch(BASE + f"/ro-RO/Train/{number}")
    codes.append(f"{st}/{secs:.2f}s")
    time.sleep(0.5)
line("statuses", "  ".join(codes))

print()
print("cookies held:", ", ".join(sorted({c.name for c in jar})) or "none")
