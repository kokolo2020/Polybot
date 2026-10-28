#!/usr/bin/env python3
"""
collect.py - READ-ONLY data collector for Polymarket BTC Up/Down markets. Places no bets, needs no keys.

Why: betting only teaches us about the bets we chose (a biased sample). This logs EVERY window, every
~30s: market prices + order book top, Binance spot/strike/volatility, time left - and later Polymarket's
real result. A few days gives thousands of unbiased labelled snapshots to test ideas out-of-sample.

  python3 collect.py                 # runs forever, Ctrl-C to stop (safe to restart)
  python3 collect.py --once          # one pass

Files (python/data/): snapshots.csv, results.csv, meta.jsonl (price-like fields in Polymarket's event JSON),
state.json (unresolved windows).
"""
import argparse, csv, json, math, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
BIN = "https://data-api.binance.vision"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
SNAP, RES, META, STATE = (os.path.join(DATA, f) for f in ("snapshots.csv", "results.csv", "meta.jsonl", "state.json"))
SNAP_COLS = ["iso", "t_ms", "kind", "slug", "start_ms", "end_ms", "tau_min", "K_binance", "S", "sig",
             "up_bid", "up_ask", "up_ask_sz", "dn_bid", "dn_ask", "dn_ask_sz"]


def get(url, params=None, timeout=12):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "polybot-collect/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def say(msg):
    print(datetime.now().strftime("%H:%M:%S"), msg, flush=True)


def parse_list(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return []
    return v or []


def btc_ctx():
    k = get(BIN + "/api/v3/klines", {"symbol": "BTCUSDT", "interval": "1m", "limit": "120"})
    opens = {c[0]: float(c[1]) for c in k}
    closes = [float(c[4]) for c in k][-61:]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mean = sum(rets) / len(rets)
    sig = max(0.0002, math.sqrt(sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)))
    return {"S": closes[-1], "sig": sig, "open": opens}


def top_of_book(token):
    b = get(CLOB + "/book", {"token_id": token})
    asks = sorted((float(x["price"]), float(x["size"])) for x in b.get("asks", []))
    bids = sorted(((float(x["price"]), float(x["size"])) for x in b.get("bids", [])), reverse=True)
    return (bids[0][0] if bids else None), (asks[0][0] if asks else None), (asks[0][1] if asks else None)


def price_like(obj, path=""):
    """Collect fields whose name looks like a reference price (priceToBeat, strike, ...), anywhere in the JSON."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{path}.{k}" if path else k
            low = k.lower()
            if any(w in low for w in ("beat", "strike", "reference", "threshold", "startprice", "openprice")) or (low == "price"):
                out[key] = v
            if isinstance(v, (dict, list)):
                out.update(price_like(v, key))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:5]):
            out.update(price_like(v, f"{path}[{i}]"))
    return out


def append_csv(path, cols, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(cols)
        w.writerow(row)


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {"open": {}, "meta_seen": []}


def save_state(st):
    with open(STATE, "w") as f:
        json.dump(st, f)


def snapshot(kinds, ctx, st):
    now = time.time()
    n = 0
    for kind in kinds:
        dur = 300 if kind == "5m" else 900
        start = int(now // dur) * dur
        slug = f"btc-updown-{kind}-{start}"
        try:
            ev = get(GAMMA + "/events", {"slug": slug})
            ev = ev[0] if isinstance(ev, list) and ev else ev
            m = ev["markets"][0]
            tokens, outcomes = parse_list(m.get("clobTokenIds")), parse_list(m.get("outcomes"))
            if len(tokens) != 2 or len(outcomes) != 2:
                continue
            up = next((j for j, o in enumerate(outcomes) if str(o).lower() == "up"), 0)
            dn = 1 - up
            ub, ua, uas = top_of_book(tokens[up])
            db, da, das = top_of_book(tokens[dn])
        except Exception as e:
            say(f"{slug}: skipped ({e})")
            continue
        end_ms, start_ms = (start + dur) * 1000, start * 1000
        K = ctx["open"].get(start_ms // 60000 * 60000, "")
        tau = round((end_ms - now * 1000) / 60000, 3)
        append_csv(SNAP, SNAP_COLS, [datetime.now(timezone.utc).isoformat(timespec="seconds"), int(now * 1000), kind, slug, start_ms, end_ms, tau,
                                     K, ctx["S"], round(ctx["sig"], 6), ub, ua, uas, db, da, das])
        n += 1
        st["open"][slug] = {"end_ms": end_ms, "up": up}
        if slug not in st["meta_seen"]:
            st["meta_seen"].append(slug)
            st["meta_seen"] = st["meta_seen"][-50:]
            with open(META, "a") as f:
                f.write(json.dumps({"slug": slug, "event": price_like(ev), "market": price_like(m),
                                    "description": str(m.get("description", ""))[:300]}) + "\n")
    return n


def resolve(st):
    now = time.time() * 1000
    for slug, w in list(st["open"].items()):
        if now < w["end_ms"] + 45000:
            continue
        if now > w["end_ms"] + 4 * 3600e3:
            del st["open"][slug]
            continue
        try:
            ev = get(GAMMA + "/events", {"slug": slug})
            ev = ev[0] if isinstance(ev, list) and ev else ev
            m = ev["markets"][0]
            prices = [float(x) for x in parse_list(m.get("outcomePrices"))]
            if not (m.get("closed") and len(prices) == 2 and max(prices) >= 0.98):
                continue
        except Exception:
            continue
        append_csv(RES, ["slug", "end_ms", "up_won"], [slug, w["end_ms"], int(prices[w["up"]] >= 0.5)])
        del st["open"][slug]
        say(f"result {slug}: Up {'WON' if prices[w['up']] >= 0.5 else 'lost'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kinds", default="5m,15m")
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    kinds = [k for k in a.kinds.split(",") if k in ("5m", "15m")]
    os.makedirs(DATA, exist_ok=True)
    say(f"collecting {kinds} every {a.interval}s into {DATA} (read-only, no bets)")
    total = 0
    while True:
        st = load_state()
        try:
            ctx = btc_ctx()
            total += snapshot(kinds, ctx, st)
            resolve(st)
            save_state(st)
            if total and total % 20 == 0:
                say(f"{total} snapshots so far")
        except Exception as e:
            say(f"loop error: {e}")
        if a.once:
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
