#!/usr/bin/env python3
"""
maker_general.py - PAPER market-making test on liquid general Polymarket markets. Places NO orders, needs no keys.

Idea: takers lose to the fee (~4%), makers pay none. So: pretend to rest bids on BOTH Yes and No at the best bid
(and 1c below) in liquid markets closing in 1-7 days. If both bids fill, we own a Yes+No pair for < $1 = locked profit.
The danger is ADVERSE SELECTION: only one side fills, and it's the side that then loses. We measure exactly that.

A bid counts as filled when real trades print through it afterwards:
  strict = some trade printed BELOW our price (surely would have matched us)
  queue  = trades at/below our price added up to more than the queue already ahead of us + our size (more realistic)

  python3 python/maker_general.py            # runs forever, Ctrl-C to stop (safe to restart)
  python3 python/maker_general.py --report   # print results so far
  python3 python/maker_general.py --once     # one pass

Files (python/data/): maker_general.csv (one row per paper quote, written when its market resolves), state_mg.json.
Only prices/sizes/timestamps are used - no wallet or profile data is read into the files.
"""
import argparse, csv, json, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATAAPI = "https://data-api.polymarket.com"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT, STATE = os.path.join(DATA, "maker_general.csv"), os.path.join(DATA, "state_mg.json")
COLS = ["market_id", "q", "kind", "side", "quote", "posted_ts", "spread", "queue_ahead", "fill_strict", "fill_queue",
        "yes_won", "hours_to_end"]
KINDS = {"join": 0.0, "off1": 0.01}       # quote at best bid, or 1c below it
MAX_OPEN = 40
MIN_VOL24 = 20000
SIZE = 20                                 # shares per paper quote (for queue maths)


def get(url, params=None, timeout=20):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; polybot-maker/1.0)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def say(m):
    print(datetime.now().strftime("%H:%M:%S"), m, flush=True)


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


def load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {"mk": {}}


def save(st):
    with open(STATE, "w") as f:
        json.dump(st, f)


def book(token):
    b = get(CLOB + "/book", {"token_id": token})
    bids = sorted(((float(x["price"]), float(x["size"])) for x in b.get("bids", [])), reverse=True)
    asks = sorted((float(x["price"]), float(x["size"])) for x in b.get("asks", []))
    return bids, asks


def discover(st):
    now = time.time()
    open_n = len(st["mk"])
    if open_n >= MAX_OPEN:
        return
    ms = get(GAMMA + "/markets", {"closed": "false", "active": "true", "order": "volume24hr", "ascending": "false", "limit": 150})
    for m in ms:
        if len(st["mk"]) >= MAX_OPEN:
            break
        mid = str(m.get("id"))
        if mid in st["mk"]:
            continue
        try:
            toks, outs = lst(m.get("clobTokenIds")), lst(m.get("outcomes"))
            end = ts(m.get("endDate"))
            if len(toks) != 2 or [str(o).lower() for o in outs] != ["yes", "no"] or not end:
                continue
            if not (now + 3 * 3600 < end < now + 7 * 86400) or float(m.get("volume24hr") or 0) < MIN_VOL24:
                continue
            (yb, ya), (nb, na) = book(toks[0]), book(toks[1])
            if not yb or not ya or not nb or not na:
                continue
            spread = round(ya[0][0] - yb[0][0], 3)
            if spread < 0.02 or not (0.05 <= yb[0][0] <= 0.95) or not (0.05 <= nb[0][0] <= 0.95):
                continue
            qs = []
            for kind, off in KINDS.items():
                for side, tok, bids in (("yes", toks[0], yb), ("no", toks[1], nb)):
                    q = round(bids[0][0] - off, 2)
                    ahead = sum(s for p, s in bids if p >= q)       # size already resting at or above our price
                    qs.append({"kind": kind, "side": side, "tok": tok, "quote": q, "ahead": round(ahead), "strict": 0,
                               "cum": 0.0})
            st["mk"][mid] = {"q": str(m.get("question", ""))[:80].replace(",", " "), "cond": m.get("conditionId"), "end": end,
                             "posted": int(now), "spread": spread, "last_ts": int(now), "qs": qs}
            say(f"quoting {m.get('slug','')[:50]} spread {spread} ({len(st['mk'])} open)")
        except Exception:
            continue


def check_fills(st):
    now = time.time()
    for mid, w in st["mk"].items():
        if now > w["end"] - 1800:          # stop counting fills 30 min before the market's end date
            continue
        try:
            tr = get(DATAAPI + "/trades", {"market": w["cond"], "limit": 500})
            tr = tr if isinstance(tr, list) else tr.get("data", [])
        except Exception:
            continue
        newest = w["last_ts"]
        for x in tr:
            try:
                t, px, sz, a = float(x["timestamp"]), float(x["price"]), float(x["size"]), str(x["asset"])
            except Exception:
                continue
            if t <= w["last_ts"] or t > w["end"] - 1800:
                continue
            newest = max(newest, t)
            for qd in w["qs"]:
                if qd["tok"] != a:
                    continue
                if px < qd["quote"]:
                    qd["strict"] = 1
                if px <= qd["quote"]:
                    qd["cum"] += sz
        w["last_ts"] = int(newest)


def resolve(st):
    now = time.time()
    for mid, w in list(st["mk"].items()):
        if now < w["end"]:
            continue
        if now > w["end"] + 5 * 86400:
            del st["mk"][mid]
            continue
        try:
            m = get(GAMMA + "/markets", {"id": mid})
            m = m[0] if isinstance(m, list) else m
            pr = [float(x) for x in lst(m.get("outcomePrices"))]
            if not (m.get("closed") and len(pr) == 2 and max(pr) >= 0.98):
                continue
            yes_won = int(pr[0] > 0.5)
        except Exception:
            continue
        new = not os.path.exists(OUT)
        with open(OUT, "a", newline="") as f:
            wr = csv.writer(f)
            if new:
                wr.writerow(COLS)
            for qd in w["qs"]:
                wr.writerow([mid, w["q"], qd["kind"], qd["side"], qd["quote"], w["posted"], w["spread"], qd["ahead"],
                             qd["strict"], int(qd["cum"] >= qd["ahead"] + SIZE), yes_won, round((w["end"] - w["posted"]) / 3600, 1)])
        del st["mk"][mid]
        say(f"resolved: {w['q'][:50]}")


def report():
    with open(OUT) as f:
        rows = list(csv.DictReader(f))
    mk = {}
    for r in rows:
        mk.setdefault(r["market_id"], []).append(r)
    print(f"{len(mk)} resolved markets, {len(rows)} paper quotes. Maker fee = 0 (rebate ignored). {SIZE} shares per quote.")
    for fillkey, name in (("fill_queue", "QUEUE-AWARE fills (realistic)"), ("fill_strict", "STRICT fills (traded below us)")):
        print(f"\n{name}")
        print(f"{'quote':<6}{'quotes':>8}{'filled':>8}{'both':>7}{'one side':>10}{'PnL/mkt $':>11}{'PnL/share filled':>18}{'one-side PnL/share':>20}")
        for kind in KINDS:
            n = fil = both = one = 0
            pnl = filled_sh = one_pnl = one_sh = 0.0
            for ms in mk.values():
                qs = [r for r in ms if r["kind"] == kind]
                if len(qs) != 2:
                    continue
                n += 2
                f = [r for r in qs if int(r[fillkey])]
                fil += len(f)
                yes_won = int(qs[0]["yes_won"])
                p = 0.0
                for r in f:
                    win = (r["side"] == "yes") == bool(yes_won)
                    pnl_i = (1.0 if win else 0.0) - float(r["quote"])
                    p += pnl_i * SIZE
                    filled_sh += SIZE
                    if len(f) == 1:
                        one_pnl += pnl_i * SIZE
                        one_sh += SIZE
                pnl += p
                both += len(f) == 2
                one += len(f) == 1
            m = len(mk)
            print(f"{kind:<6}{n:>8}{fil:>8}{both:>7}{one:>10}{pnl/max(1,m):>+11.2f}{pnl/max(1,filled_sh):>+18.3f}{one_pnl/max(1,one_sh):>+20.3f}")
    print("\nHow to read: 'both' = pair bought for <$1 (locked profit). 'one side' = directional risk; if its PnL/share is\n"
          "clearly negative, that is adverse selection eating the spread. Need 100+ markets before believing any of it.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=120)
    a = ap.parse_args()
    os.makedirs(DATA, exist_ok=True)
    if a.report:
        report() if os.path.exists(OUT) else print("no resolved markets yet")
        return
    say(f"paper maker test running (no orders). every {a.interval}s, up to {MAX_OPEN} markets")
    while True:
        st = load()
        try:
            discover(st)
            check_fills(st)
            resolve(st)
            save(st)
        except Exception as e:
            say(f"loop error: {e}")
        if a.once:
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
