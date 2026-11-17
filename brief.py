#!/usr/bin/env python3
"""
Lodara daily Montra brief.

Pulls public data from Montra's API, builds a short brief, and sends it to
your Telegram so you can review it and post it on X.

Needs two GitHub secrets: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
Run locally without them and it just prints the brief.

Uses only the Python standard library, so nothing needs installing.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------- settings
API = "https://api.montramarkets.xyz"
TOP_N = 5                      # markets shown in "Top mcap"
MIN_MOVER_VOLUME_USD = 1000    # a token needs this much 24h volume to count as a mover
MOVERS_EACH_WAY = 2            # biggest gainers and biggest losers shown
PAGES_PER_LIST = 3             # pages fetched for each ranked list
MAX_POST_CHARS = 280
LOCAL_TZ = "America/Chicago"

# Sort values to try. Montra's docs list a sort filter but not every value,
# so the script tests these and keeps the first one that really sorts.
MCAP_SORTS = ["marketCap", "mcap", "marketcap", "market_cap", "cap"]
VOLUME_SORTS = ["volume", "volume24h", "vol", "trending", "hot"]
GRADUATED_SORT = "graduated"

DAY_MS = 24 * 60 * 60 * 1000


def log(msg):
    print(f"[brief] {msg}", flush=True)


# ---------------------------------------------------------------- http
def get(path, params=None, retries=5):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"User-Agent": "lodara-brief/1.0", "Accept": "application/json"}
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                wait = 2 ** attempt * 2
                log(f"{url} returned {e.code}, retrying in {wait}s")
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            wait = 2 ** attempt * 2
            log(f"{url} failed ({e}), retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"Montra API kept failing: {url}")


def token_pages(params, pages=PAGES_PER_LIST):
    """Fetch a few pages of /api/tokens with the given filters."""
    out = []
    for page in range(1, pages + 1):
        data = get("/api/tokens", {**params, "page": page, "limit": 50})
        toks = data.get("tokens") or []
        out.extend(toks)
        if page >= int(data.get("pages") or 1) or not toks:
            break
    return out


# ---------------------------------------------------------------- helpers
def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def sorted_desc(values):
    vals = [v for v in values if v is not None]
    return (
        len(vals) >= 3
        and vals[0] != vals[-1]
        and all(a >= b for a, b in zip(vals, vals[1:]))
    )


def find_working_sort(candidates, field):
    """Return (sort_value, tokens) for the first candidate that sorts by field."""
    for s in candidates:
        try:
            toks = token_pages({"sort": s}, pages=1)
        except Exception as e:  # one bad candidate shouldn't stop the run
            log(f"sort={s} failed: {e}")
            continue
        if sorted_desc([num(t.get(field)) for t in toks]):
            log(f"sort={s} works for {field} ({len(toks)} tokens per page)")
            return s, token_pages({"sort": s})
        log(f"sort={s} does not sort by {field}")
    return None, []


def money(v):
    v = v or 0
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            s = f"{v / div:.1f}".rstrip("0").rstrip(".")
            return f"${s}{suffix}"
    return f"${v:,.0f}"


def load_handles():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "handles.json")
    try:
        with open(path) as f:
            return {k.lower(): v for k, v in json.load(f).items()}
    except FileNotFoundError:
        return {}


def label(token, handles):
    """$SYMBOL, or the project's @handle if it's in handles.json."""
    handle = handles.get((token.get("address") or "").lower())
    if handle:
        return handle if handle.startswith("@") else "@" + handle
    return "$" + (token.get("symbol") or "?")


def local_date():
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(LOCAL_TZ))
    except Exception:
        now = datetime.now(timezone.utc)
    return f"{now:%b} {now.day}"


# ---------------------------------------------------------------- data
def collect():
    stats = get("/api/stats")
    for key in ("volume24hUsd", "fees24hUsd", "launched24h"):
        if num(stats.get(key)) is None:
            raise RuntimeError(f"/api/stats is missing {key}")
    if num(stats["volume24hUsd"]) <= 0:
        raise RuntimeError("/api/stats shows zero 24h volume, which looks wrong")

    pool = {}

    def add(toks):
        for t in toks:
            if t.get("address"):
                pool[t["address"].lower()] = t

    mcap_sort, mcap_list = find_working_sort(MCAP_SORTS, "marketCapUsd")
    add(mcap_list)
    _, vol_list = find_working_sort(VOLUME_SORTS, "volume24hUsd")
    add(vol_list)

    grad_list = token_pages({"sort": GRADUATED_SORT, "stage": "graduated"})
    grad_list = [t for t in grad_list if (t.get("graduation") or {}).get("stage") == "graduated"]
    log(f"graduated tokens fetched: {len(grad_list)}")
    add(grad_list)

    tokens = list(pool.values())
    log(f"token pool size: {len(tokens)}")

    top = sorted(tokens, key=lambda t: num(t.get("marketCapUsd")) or 0, reverse=True)[:TOP_N]
    if len(top) < TOP_N:
        raise RuntimeError(f"only found {len(top)} tokens for the top list")
    if not mcap_sort:
        log("WARNING: no market cap sort worked, top list built from the pool only")

    movers = [
        t for t in tokens
        if (num(t.get("volume24hUsd")) or 0) >= MIN_MOVER_VOLUME_USD
        and num(t.get("change24hPct")) is not None
    ]
    movers.sort(key=lambda t: num(t["change24hPct"]), reverse=True)
    gainers = [t for t in movers if num(t["change24hPct"]) > 0][:MOVERS_EACH_WAY]
    losers = [t for t in reversed(movers) if num(t["change24hPct"]) < 0][:MOVERS_EACH_WAY]

    cutoff = time.time() * 1000 - DAY_MS
    graduates = [
        t for t in grad_list
        if num((t.get("graduation") or {}).get("graduatedAt")) is not None
        and num(t["graduation"]["graduatedAt"]) >= cutoff
    ]
    graduates.sort(key=lambda t: num(t["graduation"]["graduatedAt"]), reverse=True)

    return stats, top, gainers, losers, graduates


# ---------------------------------------------------------------- text
def build_posts(stats, top, gainers, losers, graduates, handles):
    head = (
        f"Montra daily brief · {local_date()}\n"
        f"Volume {money(num(stats['volume24hUsd']))} · "
        f"Fees {money(num(stats['fees24hUsd']))} · "
        f"Launches {int(num(stats['launched24h'])):,}"
    )
    top_line = "Top mcap: " + ", ".join(
        f"{label(t, handles)} {money(num(t.get('marketCapUsd')))}" for t in top
    )
    mover_bits = [
        f"{label(t, handles)} {num(t['change24hPct']):+.0f}%" for t in gainers + losers
    ]
    movers_line = (
        "Movers: " + ", ".join(mover_bits)
        if mover_bits
        else f"Movers: none above {money(MIN_MOVER_VOLUME_USD)} volume"
    )
    if graduates:
        names = [label(t, handles) for t in graduates[:5]]
        extra = len(graduates) - len(names)
        grad_line = "Graduated: " + ", ".join(names) + (f" +{extra} more" if extra > 0 else "")
    else:
        grad_line = "Graduated: none in the last 24h"

    single = "\n".join([head, top_line, movers_line, grad_line])
    if len(single) <= MAX_POST_CHARS:
        return [single]
    return ["\n".join([head, top_line]), "\n".join([movers_line, grad_line])]


# ---------------------------------------------------------------- telegram
def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("Telegram secrets not set, printing only")
        return
    body = urllib.parse.urlencode(
        {"chat_id": chat, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        if json.load(resp).get("ok") is not True:
            raise RuntimeError("Telegram did not accept the message")


def main():
    try:
        handles = load_handles()
        posts = build_posts(*collect(), handles)
    except Exception as e:
        log(f"FAILED: {e}")
        try:
            send_telegram(
                "Montra brief skipped today.\n"
                f"Reason: {e}\n"
                "Open the GitHub Actions log for details. Nothing was posted."
            )
        except Exception as te:
            log(f"could not send the failure alert: {te}")
        sys.exit(1)

    if len(posts) == 1:
        message = posts[0]
    else:
        message = "\n\n".join(f"Post {i}/{len(posts)}\n{p}" for i, p in enumerate(posts, 1))
    for i, p in enumerate(posts, 1):
        log(f"post {i}: {len(p)} characters")
    print("\n" + message + "\n")
    send_telegram(message)
    log("done")


if __name__ == "__main__":
    main()
