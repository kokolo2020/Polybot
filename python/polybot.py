#!/usr/bin/env python3
"""
Polymarket PAPER trading bot. No wallet, no real orders.

- Reads markets from the Gamma API and order books from the CLOB API (public, no key).
- Simulates fills by walking the real order book (so slippage and depth limits apply).
- Sizes with fractional Kelly, capped per trade and by available depth.
- State is saved to state.json; every trade is appended to trades.csv.

Usage:
  python polybot.py --once          # one scan (good for cron / n8n Execute Command)
  python polybot.py --loop 60       # scan every 60 seconds
  python polybot.py --status        # print equity and open positions
"""
import argparse
import csv
import json
import os
import time
import urllib.parse
import urllib.request

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

CFG = {
    "start_bank": 1000.0,
    "markets_per_scan": 40,       # top markets by 24h volume
    "min_liquidity": 5000.0,      # skip thin markets
    "min_price": 0.08,            # skip extreme longshots / near-certain
    "max_price": 0.92,
    "min_edge": 0.02,             # model prob must beat ask by 2 cents
    "imbalance_weight": 0.04,     # PLACEHOLDER signal strength, replace with your own model
    "kelly_fraction": 0.25,
    "max_pos_pct": 0.05,          # max 5% of equity per position
    "max_open": 12,
    "take_profit": 0.05,          # exit when bid is 5c above entry
    "stop_loss": 0.04,            # exit when bid is 4c below entry
    "fee_rate": 0.0,              # set if the market charges fees
    "book_levels": 5,
}

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
TRADES_FILE = os.path.join(HERE, "trades.csv")


def get_json(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "polybot-paper/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"cash": CFG["start_bank"], "positions": {}, "realized": 0.0, "scans": 0}


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=2)


def log_trade(row):
    new = not os.path.exists(TRADES_FILE)
    with open(TRADES_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "action", "question", "outcome", "price", "shares", "cash_delta", "note"])
        w.writerow(row)


def parse_list(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return []
    return v or []


def fetch_markets():
    data = get_json(f"{GAMMA}/markets", {
        "active": "true", "closed": "false",
        "limit": CFG["markets_per_scan"],
        "order": "volume24hr", "ascending": "false",
    })
    out = []
    for m in data:
        tokens = parse_list(m.get("clobTokenIds"))
        outcomes = parse_list(m.get("outcomes"))
        if len(tokens) != 2 or len(outcomes) != 2:
            continue
        if float(m.get("liquidity") or 0) < CFG["min_liquidity"]:
            continue
        out.append({"q": m.get("question", "?"), "tokens": tokens, "outcomes": outcomes})
    return out


def fetch_book(token_id):
    b = get_json(f"{CLOB}/book", {"token_id": token_id})
    bids = sorted(((float(x["price"]), float(x["size"])) for x in b.get("bids", [])), reverse=True)
    asks = sorted((float(x["price"]), float(x["size"])) for x in b.get("asks", []))
    return bids, asks


def walk_buy(asks, usd):
    """Spend up to usd walking the asks. Returns (shares, cost, avg_price)."""
    shares = cost = 0.0
    for price, size in asks:
        take_usd = min(usd - cost, price * size)
        if take_usd <= 0:
            break
        shares += take_usd / price
        cost += take_usd
    return shares, cost, (cost / shares if shares else 0.0)


def walk_sell(bids, shares):
    """Sell shares into the bids. Returns (sold, proceeds)."""
    sold = proceeds = 0.0
    for price, size in bids:
        s = min(shares - sold, size)
        if s <= 0:
            break
        sold += s
        proceeds += s * price
    return sold, proceeds


def model_prob(bids, asks):
    """PLACEHOLDER signal: mid price nudged by top-of-book imbalance.
    This is NOT a proven edge. Replace with your own probability model."""
    n = CFG["book_levels"]
    bd = sum(p * s for p, s in bids[:n])
    ad = sum(p * s for p, s in asks[:n])
    if bd + ad == 0:
        return None
    imb = (bd - ad) / (bd + ad)
    mid = (bids[0][0] + asks[0][0]) / 2
    return min(0.99, max(0.01, mid + CFG["imbalance_weight"] * imb))


def equity(st, marks):
    eq = st["cash"]
    for tok, pos in st["positions"].items():
        eq += pos["shares"] * marks.get(tok, pos["entry"])
    return eq


def scan(st):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    marks = {}
    markets = fetch_markets()
    books = {}

    # 1) manage exits
    for tok, pos in list(st["positions"].items()):
        try:
            bids, asks = fetch_book(tok)
        except Exception as e:
            print("book error", tok[:8], e)
            continue
        if not bids:
            continue
        books[tok] = (bids, asks)
        bid = bids[0][0]
        marks[tok] = bid
        p = model_prob(bids, asks) if asks else None
        reason = None
        if bid - pos["entry"] >= CFG["take_profit"]:
            reason = "take_profit"
        elif pos["entry"] - bid >= CFG["stop_loss"]:
            reason = "stop_loss"
        elif p is not None and p < bid - 0.01:
            reason = "edge_gone"
        if reason:
            sold, proceeds = walk_sell(bids, pos["shares"])
            proceeds *= (1 - CFG["fee_rate"])
            if sold <= 0:
                continue
            pnl = proceeds - sold * pos["entry"]
            st["cash"] += proceeds
            st["realized"] += pnl
            pos["shares"] -= sold
            log_trade([now, "SELL", pos["q"], pos["outcome"], round(proceeds / sold, 4),
                       round(sold, 2), round(proceeds, 2), f"{reason} pnl={pnl:.2f}"])
            if pos["shares"] < 0.01:
                del st["positions"][tok]

    # 2) look for entries
    eq = equity(st, marks)
    for m in markets:
        if len(st["positions"]) >= CFG["max_open"]:
            break
        for tok, outcome in zip(m["tokens"], m["outcomes"]):
            if tok in st["positions"]:
                continue
            try:
                bids, asks = books.get(tok) or fetch_book(tok)
            except Exception as e:
                print("book error", tok[:8], e)
                continue
            if not bids or not asks:
                continue
            ask = asks[0][0]
            if not (CFG["min_price"] <= ask <= CFG["max_price"]):
                continue
            p = model_prob(bids, asks)
            if p is None:
                continue
            edge = p - ask - ask * CFG["fee_rate"]
            if edge < CFG["min_edge"]:
                continue
            kelly = edge / (1 - ask)
            usd = min(eq * CFG["kelly_fraction"] * kelly, eq * CFG["max_pos_pct"], st["cash"])
            if usd < 5:
                continue
            shares, cost, avg = walk_buy(asks, usd)
            if shares <= 0:
                continue
            cost_total = cost * (1 + CFG["fee_rate"])
            if cost_total > st["cash"]:
                continue
            st["cash"] -= cost_total
            st["positions"][tok] = {"q": m["q"], "outcome": outcome, "shares": shares,
                                    "entry": avg, "opened": now}
            marks[tok] = bids[0][0]
            log_trade([now, "BUY", m["q"], outcome, round(avg, 4), round(shares, 2),
                       round(-cost_total, 2), f"p_model={p:.3f} edge={edge:.3f}"])
            break  # one side per market

    st["scans"] += 1
    eq = equity(st, marks)
    print(f"[{now}] equity=${eq:,.2f} cash=${st['cash']:,.2f} open={len(st['positions'])} "
          f"realized=${st['realized']:,.2f} scans={st['scans']}")


def status(st):
    marks = {}
    for tok in st["positions"]:
        try:
            bids, _ = fetch_book(tok)
            if bids:
                marks[tok] = bids[0][0]
        except Exception:
            pass
    print(f"Equity ${equity(st, marks):,.2f} | cash ${st['cash']:,.2f} | realized ${st['realized']:,.2f}")
    for tok, p in st["positions"].items():
        mk = marks.get(tok, p["entry"])
        print(f"  {p['outcome']:>4} {p['shares']:8.1f} @ {p['entry']:.3f} -> {mk:.3f} | {p['q'][:60]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", type=int, metavar="SECONDS")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    st = load_state()
    if a.status:
        status(st)
    elif a.loop:
        while True:
            try:
                scan(st)
                save_state(st)
            except Exception as e:
                print("scan error:", e)
            time.sleep(a.loop)
    else:
        scan(st)
        save_state(st)


if __name__ == "__main__":
    main()
