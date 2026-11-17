#!/usr/bin/env python3
"""Add the latest copper, lithium, WTI and Brent prices to data.json.

Run every weekday at 10:30 IST by .github/workflows/update-prices.yml (ESS Pricing Tracker).

Sources (both free, no key needed):
  - Copper, WTI, Brent: Yahoo Finance front-month futures (COMEX HG=F, NYMEX CL=F, ICE BZ=F)
  - Lithium carbonate:  Sina Finance, GFEX lithium carbonate main contract (LC0), CNY / tonne
    (the same contract Trading Economics quotes as LC:COM)

Trading Economics now loads its price table behind a bot check, so it can no longer be read
by this job (that is why updates stopped on 16 Nov 2026).

Each price is stored with Day / Week / Month / Year % change calculated from the source's own
daily history, and marked "TE" (the dashboard's "updated automatically" marker). A date that
already has an entry you made yourself is never overwritten.
"""
import datetime as dt
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"

YAHOO = {"copper": "HG=F", "wti": "CL=F", "brent": "BZ=F"}
SINA = {"lithium": "LC0"}


def get(url, referer=None, tries=3):
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if referer:
        headers["Referer"] = referer
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as res:
                return res.read().decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001 - retry any network error
            if attempt == tries - 1:
                raise
            print(f"  retrying {url} ({e})")
            time.sleep(5 * (attempt + 1))


def yahoo(symbol):
    """Daily closes for the last ~13 months as [(date, close)], oldest first."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=13mo&interval=1d"
    res = json.loads(get(url))["chart"]["result"][0]
    tz = dt.timezone(dt.timedelta(seconds=res["meta"].get("gmtoffset", 0)))
    closes = res["indicators"]["quote"][0]["close"]
    bars = [(t, c) for t, c in zip(res["timestamp"], closes) if c is not None]
    live = res["meta"].get("regularMarketPrice")
    when = res["meta"].get("regularMarketTime")
    if live and when and bars and bars[-1][0] >= when - 60:
        bars = bars[:-1]  # the last bar is the live quote, added below with its own date
    out = {}
    for t, c in bars:  # settled sessions, dated in the exchange's time zone
        out[dt.datetime.fromtimestamp(t, tz).date()] = c
    if live and when:
        # Futures trade overnight, so date the live quote by UTC (today, when this job runs).
        out[dt.datetime.fromtimestamp(when, dt.timezone.utc).date()] = live
    return sorted(out.items())


def sina(symbol):
    """Daily closes for a China futures contract from Sina Finance as [(date, close)], oldest first."""
    url = f"https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/InnerFuturesNewService.getDailyKLine?symbol={symbol}"
    text = get(url, referer="https://finance.sina.com.cn/")
    rows = json.loads(re.search(r"\((\[.*\])\)", text, re.S).group(1))
    return [(dt.date.fromisoformat(r["d"]), float(r["c"])) for r in rows]


def before(history, day):
    """Close on the last trading day on or before `day`."""
    older = [c for d, c in history if d <= day]
    return older[-1] if older else None


def changes(history):
    """(date, price, [day %, week %, month %, year %]) for the latest point, as fractions."""
    last_d, last = history[-1]
    prev = history[-2][1] if len(history) > 1 else None
    refs = [prev,
            before(history, last_d - dt.timedelta(days=7)),
            before(history, last_d - dt.timedelta(days=30)),
            before(history, last_d - dt.timedelta(days=365))]
    pct = [round(last / r - 1, 6) if r else None for r in refs]
    return last_d, round(last, 4), pct


def fetch_all():
    found = {}
    for key, sym in YAHOO.items():
        try:
            found[key] = changes(yahoo(sym))
        except Exception as e:  # noqa: BLE001
            print(f"{key}: could not fetch from Yahoo Finance ({e})")
    for key, sym in SINA.items():
        try:
            found[key] = changes(sina(sym))
        except Exception as e:  # noqa: BLE001
            print(f"{key}: could not fetch from Sina Finance ({e})")
    return found


MATCH = {"copper": r"copper", "lithium": r"lithium", "wti": r"\bwti\b|crude", "brent": r"brent"}


def targets(data):
    """Which dashboard section each price goes to: your own imported section with a matching
    name (e.g. "Crude Oil"), otherwise the built-in one — unless you've hidden it."""
    hidden = data.get("hiddenSections", {})
    own = sorted(data.get("sections", {}).items(), key=lambda kv: kv[1].get("order", 0))
    out = {}
    for builtin, pattern in MATCH.items():
        names = [k for k, s in own if s.get("segment") != "ess" and re.search(pattern, s.get("name", ""), re.I)
                 and not (builtin == "wti" and re.search("brent", s.get("name", ""), re.I))]
        if names:
            out[builtin] = names[0]
        elif builtin in data.get("sections", {}) and not hidden.get(builtin):
            out[builtin] = builtin
    return out


def main():
    found = fetch_all()
    if not found:
        sys.exit("No prices found — Yahoo Finance and Sina Finance could not be reached.")

    data = json.loads(DATA.read_text()) if DATA.exists() else {}
    data.setdefault("sections", {})
    data.setdefault("hideSeed", {})
    data.setdefault("hiddenSections", {})
    rows = data.setdefault("rows", {})
    where = targets(data)

    changed = False
    for name, (date, price, pct) in found.items():
        key = where.get(name)
        if not key:
            print(f"{name}: skipped — there is no {name} row on this dashboard")
            continue
        label = data["sections"].get(key, {}).get("name", name)
        day = rows.setdefault(key, {})
        current = day.get(date.isoformat())
        if current and not (len(current) > 5 and current[5] == "TE"):
            print(f"{label}: {date} already has your own entry — left as is")
            continue
        new = [price, *pct, "TE"]
        if current != new:
            day[date.isoformat()] = new
            changed = True
        print(f"{label}: {date} {price}")

    if changed:
        data["savedAt"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        DATA.write_text(json.dumps(data, separators=(",", ":")))
    print("data.json updated" if changed else "No new prices")
    missing = sorted((set(YAHOO) | set(SINA)) - set(found))
    if missing:
        # A warning, not a failure, so the prices that did come through are still saved.
        print(f"::warning::Could not fetch: {', '.join(missing)} — see the log above.")


if __name__ == "__main__":
    main()
