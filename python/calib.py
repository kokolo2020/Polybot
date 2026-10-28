#!/usr/bin/env python3
"""
calib.py - READ-ONLY calibration study on RESOLVED Polymarket markets. Places no bets, needs no keys.

Question: when a market is priced at X cents N days before it closes, how often does it really resolve Yes?
Where real win rate differs from price (by more than fees), there might be an edge (e.g. longshots too expensive).

  python3 python/calib.py              # collect (resumable, ~10-20 min), then print the report
  python3 python/calib.py --report     # just re-print the report from python/data/calib.csv
  python3 python/calib.py --events 400 # smaller/faster run

Output: python/data/calib.csv  (send me this file). Binary Yes/No markets only, volume >= $5k.
"""
import argparse, csv, json, os, sys, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(DATA, "calib.csv")
COLS = ["id", "slug", "category", "end_ts", "volume", "p_1d", "p_7d", "yes_won"]
PRIORITY = ["Politics", "Sports", "Crypto", "Business", "Science", "Tech", "Culture", "Geopolitics", "Economy"]


def get(url, params=None, timeout=20):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; polybot-calib/1.0)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def lst(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return []
    return v or []


def ts(s):
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def category(ev):
    labels = [t.get("label", "") for t in (ev.get("tags") or []) if isinstance(t, dict)]
    for p in PRIORITY:
        if p in labels:
            return p
    labels = [l for l in labels if l and l.lower() != "all"]
    return labels[0] if labels else "Other"


def price_at(hist, cut, max_gap):
    p = None
    for x in hist:
        if x["t"] <= cut:
            p, tt = x["p"], x["t"]
    return p if p is not None and cut - tt <= max_gap else None


def work(item):
    ev, m = item
    try:
        toks, outs, pr = lst(m.get("clobTokenIds")), lst(m.get("outcomes")), [float(x) for x in lst(m.get("outcomePrices"))]
        if len(toks) != 2 or [str(o).lower() for o in outs] != ["yes", "no"] or max(pr) < 0.98:
            return None
        vol = float(m.get("volume") or 0)
        if vol < 5000:
            return None
        ends = [x for x in (ts(m.get("endDate")), ts(m.get("closedTime"))) if x]
        end = min(ends) if ends else None
        if not end or end > time.time():
            return None
        h = get(CLOB + "/prices-history", {"market": toks[0], "startTs": end - 9 * 86400, "endTs": end, "fidelity": 360}).get("history", [])
        p1, p7 = price_at(h, end - 86400, 2 * 86400), price_at(h, end - 7 * 86400, 2 * 86400)
        if p1 is None and p7 is None:
            return None
        return [m.get("id"), m.get("slug"), category(ev), end, round(vol), "" if p1 is None else p1, "" if p7 is None else p7, int(pr[0] > 0.5)]
    except Exception:
        return None


def collect(max_events):
    os.makedirs(DATA, exist_ok=True)
    done = set()
    if os.path.exists(OUT):
        with open(OUT) as f:
            done = {r["id"] for r in csv.DictReader(f)}
    new = not os.path.exists(OUT)
    f = open(OUT, "a", newline="")
    w = csv.writer(f)
    if new:
        w.writerow(COLS)
    off, n = 0, 0
    while off < max_events:
        try:
            evs = get(GAMMA + "/events", {"closed": "true", "order": "volume", "ascending": "false", "limit": 50, "offset": off})
        except Exception as e:
            print("events page failed:", e)
            break
        if not evs:
            break
        items = [(ev, m) for ev in evs for m in ev.get("markets", []) if str(m.get("id")) not in done]
        with ThreadPoolExecutor(6) as ex:
            for row in ex.map(work, items):
                if row:
                    w.writerow(row)
                    n += 1
        f.flush()
        off += 50
        print(f"events {off}/{max_events}  rows added {n}", flush=True)
        time.sleep(0.5)
    f.close()


def table(rows, key, title):
    print(f"\n{title}")
    print(f"{'price band':<12}{'n':>6}{'avg price':>11}{'won':>7}{'gap':>7}{'ROI gross':>11}{'ROI +7% fee':>13}")
    bands = [(0, .05), (.05, .1), (.1, .2), (.2, .35), (.35, .5), (.5, .65), (.65, .8), (.8, .9), (.9, .95), (.95, 1.01)]
    for lo, hi in bands:
        s = [(r[key], r["yes_won"]) for r in rows if r[key] is not None and lo <= r[key] < hi]
        if len(s) < 15:
            continue
        n = len(s)
        pm = sum(p for p, _ in s) / n
        wr = sum(y for _, y in s) / n
        # buy Yes at price p: payoff y - p ; the other side (buy No at 1-p) is the mirror
        roi = sum(y - p for p, y in s) / sum(p for p, _ in s)
        roif = sum(y - p - 0.07 * p * (1 - p) for p, y in s) / sum(p + 0.07 * p * (1 - p) for p, _ in s)
        print(f"{lo:.2f}-{min(hi,1):.2f}{'':<3}{n:>6}{pm:>11.3f}{wr:>7.3f}{wr-pm:>+7.3f}{roi:>+11.1%}{roif:>+13.1%}")


def report():
    with open(OUT) as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["end_ts"] = int(r["end_ts"]); r["yes_won"] = int(r["yes_won"])
        for k in ("p_1d", "p_7d"):
            r[k] = float(r[k]) if r[k] != "" else None
    rows.sort(key=lambda r: r["end_ts"])
    print(f"{len(rows)} resolved markets, {rows[0]['end_ts']} .. {rows[-1]['end_ts']}")
    for key, name in (("p_1d", "1 day before close"), ("p_7d", "7 days before close")):
        table(rows, key, f"ALL markets, price {name} (gap = real win rate minus price; ROI = buying Yes at that price)")
    half = len(rows) // 2
    for nm, part in (("OLDER half (find patterns here)", rows[:half]), ("NEWER half (confirm here)", rows[half:])):
        table(part, "p_1d", f"{nm}, 1 day before close")
    cats = {}
    for r in rows:
        cats.setdefault(r["category"], []).append(r)
    for c, part in sorted(cats.items(), key=lambda kv: -len(kv[1]))[:5]:
        if len(part) >= 100:
            table(part, "p_1d", f"CATEGORY {c} ({len(part)} markets), 1 day before close")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--events", type=int, default=1500)
    a = ap.parse_args()
    if not a.report:
        collect(a.events)
    if os.path.exists(OUT):
        report()
    else:
        print("no data collected")


if __name__ == "__main__":
    main()
