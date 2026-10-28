#!/usr/bin/env python3
"""
polybot_live.py - Polymarket BTC Up/Down (5m / 15m) bot. DRY RUN by default.

Same model as the paper web app (Polybot Desk): fair value from Binance spot, edge = p - ask - fee,
walk the real order book, skip thin books / slippage.

  python3 polybot_live.py                 # dry run: no keys, no orders, logs what it WOULD do + real results
  python3 polybot_live.py --probe SLUG    # (needs keys) print the SDK's market object. No trading.
  python3 polybot_live.py --live          # real orders. Needs keys + POLYBOT_CONFIRM_LIVE=yes

Only run this from a country where Polymarket allows you to open positions, on your OWN account
with your OWN money. It checks Polymarket's geoblock endpoint and refuses to trade if blocked.
Never share your private key with anyone. Paper results are not a proven edge. You can lose all of it.
"""
import argparse, asyncio, csv, json, math, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
BIN = "https://data-api.binance.vision"
GEO = "https://polymarket.com/api/geoblock"
FEE = 0.07            # taker fee rate used by the model (same as the web app)
HARD_MAX_STAKE = 25.0  # absolute ceiling per bet, even if you pass a bigger --stake
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "live_state.json")
LOG_FILE = os.path.join(HERE, "live_log.csv")
STOP_FILE = os.path.join(HERE, "STOP")   # create this file to stop new bets immediately


def get(url, params=None, timeout=12):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "polybot-live/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def say(msg):
    print(datetime.now().strftime("%H:%M:%S"), msg, flush=True)


# ---------- model (port of the web app) ----------
def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def btc_ctx():
    k = get(BIN + "/api/v3/klines", {"symbol": "BTCUSDT", "interval": "1m", "limit": "120"})
    opens = {c[0]: float(c[1]) for c in k}
    closes = [float(c[4]) for c in k][-61:]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mean = sum(rets) / len(rets)
    sig = max(0.0002, math.sqrt(sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)))
    return {"S": closes[-1], "sig": sig, "open": opens}


def p_up(ctx, K, end_ms):
    tau = max(0.1, (end_ms - time.time() * 1000) / 60000)
    z = math.log(ctx["S"] / K) / (ctx["sig"] * math.sqrt(tau))
    return min(0.98, max(0.02, phi(z)))


def fee(shares, px):
    return shares * FEE * px * (1 - px)


def walk_buy(asks, usd):
    shares = cost = 0.0
    for p, s in asks:
        take = min(usd - cost, p * s)
        if take <= 0:
            break
        shares += take / p
        cost += take
    return shares, cost, (cost / shares if shares else 0.0)


def get_book(token):
    b = get(CLOB + "/book", {"token_id": token})
    asks = sorted([(float(x["price"]), float(x["size"])) for x in b.get("asks", [])])
    bids = sorted([(float(x["price"]), float(x["size"])) for x in b.get("bids", [])], reverse=True)
    return bids, asks


def parse_list(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return []
    return v or []


def parse_end(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000


# ---------- markets ----------
def find_markets(kinds, ctx):
    now = time.time()
    out = []
    for kind in kinds:
        dur = 300 if kind == "5m" else 900
        base = int(now // dur) * dur
        for i in (0, 1):
            slug = f"btc-updown-{kind}-{base + i * dur}"
            try:
                ev = get(GAMMA + "/events", {"slug": slug})
                ev = ev[0] if isinstance(ev, list) and ev else ev
                m = ev["markets"][0]
            except Exception:
                continue
            tokens, outcomes = parse_list(m.get("clobTokenIds")), parse_list(m.get("outcomes"))
            if len(tokens) != 2 or len(outcomes) != 2 or m.get("closed"):
                continue
            end = parse_end(m.get("endDate") or ev.get("endDate"))
            if end < (now + 60) * 1000:
                continue
            K = ctx["open"].get(int((end - dur * 1000) // 60000 * 60000))
            if not K:
                continue
            up = next((j for j, o in enumerate(outcomes) if str(o).lower() == "up"), 0)
            out.append({"kind": kind, "slug": slug, "tokens": tokens, "outcomes": outcomes, "end": end, "K": K, "up": up})
    return out


def decide(m, ctx, cfg, books):
    """Return a bet dict or (None, reason). Same checks as the paper desk."""
    tau = (m["end"] - time.time() * 1000) / 60000
    if tau < 1.5:
        return None, "too close to the end"
    pu = p_up(ctx, m["K"], m["end"])
    best = None
    for k in (0, 1):
        bids, asks = books[k]
        if not asks or not bids:
            continue
        ask = asks[0][0]
        p = pu if k == m["up"] else 1 - pu
        edge = p - ask - FEE * ask * (1 - ask)
        if cfg["pmin"] <= ask <= cfg["pmax"] and edge >= cfg["min_edge"]:
            if not best or edge > best["edge"]:
                best = {"k": k, "ask": ask, "p": p, "edge": edge, "asks": asks}
    if not best:
        return None, "no edge"
    stake = cfg["stake"] if best["ask"] >= 0.10 else min(cfg["stake"], cfg["low_cap"])
    if stake < 1:
        return None, "stake too small"
    shares, cost, avg = walk_buy(best["asks"], stake)
    if cost < stake * 0.9:
        return None, "thin book"
    if avg - best["ask"] > 0.02:
        return None, "slippage"
    if best["p"] - avg - FEE * avg * (1 - avg) < cfg["min_edge"]:
        return None, "edge gone at fill price"
    return {**best, "stake": stake, "avg": avg, "shares": shares}, ""


# ---------- state / log ----------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"bets": {}}


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=1)


def log_row(row):
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "event", "mode", "kind", "slug", "outcome", "ask", "avg", "p", "edge", "stake", "order_id", "result", "pnl"])
        w.writerow(row)


def settle(st):
    """Look up Polymarket's own result for finished bets."""
    now = time.time() * 1000
    for slug, b in st["bets"].items():
        if b.get("result") or now < b["end"] + 30000:
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
        won = prices[b["k"]] >= 0.5
        cost = b["avg"] * b["shares"] + fee(b["shares"], b["avg"])
        b["result"] = "win" if won else "loss"
        b["pnl"] = round((b["shares"] - cost) if won else -cost, 2)
        say(f"RESULT {slug} {b['outcome']} -> {b['result'].upper()} {b['pnl']:+.2f}")
        log_row([datetime.now().isoformat(timespec="seconds"), "result", b["mode"], b["kind"], slug, b["outcome"],
                 b["ask"], b["avg"], round(b["p"], 3), round(b["edge"], 3), b["stake"], b.get("order_id", ""), b["result"], b["pnl"]])


def risk_used(st):
    today = datetime.now(timezone.utc).date().isoformat()
    realized = sum(b.get("pnl", 0) for b in st["bets"].values() if b.get("result") and b["day"] == today)
    open_cost = sum(b["stake"] for b in st["bets"].values() if not b.get("result"))
    return max(0.0, -realized) + open_cost


# ---------- live (official SDK) ----------
async def make_client():
    from polymarket import AsyncSecureClient   # pip install polymarket-client  (name per Polymarket's docs)
    return await AsyncSecureClient.create(
        private_key=os.environ["POLYMARKET_PRIVATE_KEY"], wallet=os.environ["POLYMARKET_WALLET_ADDRESS"])


def resolve_asset_id(market, label):
    """Map 'Up'/'Down' to the SDK's asset id. The docs only show yes/no, so this tries a few shapes and fails loudly."""
    ver = getattr(market, "version", None)
    outs = getattr(market, "outcomes", None)
    cands = list(outs) if isinstance(outs, (list, tuple)) else [getattr(outs, n, None) for n in ("up", "down", "yes", "no")]
    for o in cands:
        if o is None:
            continue
        name = str(getattr(o, "label", None) or getattr(o, "name", None) or getattr(o, "outcome", None) or "")
        if name.lower() == label.lower():
            return o.position_id if ver == "v2" else o.token_id
    raise RuntimeError(f"Could not map outcome '{label}' to an asset id. Run --probe and send the output.")


async def live_buy(client, slug, label, stake):
    market = await client.get_market(slug=slug)
    asset_id = resolve_asset_id(market, label)
    resp = await client.place_market_order(asset_id=asset_id, side="BUY", amount=str(stake))
    if not resp.ok:
        raise RuntimeError(resp.message)
    return resp.order_id


async def probe(slug):
    client = await make_client()
    m = await client.get_market(slug=slug)
    print("type:", type(m))
    print("attrs:", [a for a in dir(m) if not a.startswith("_")])
    for a in ("version", "condition_id", "outcomes"):
        print(a, "=", repr(getattr(m, a, None)))
    outs = getattr(m, "outcomes", None)
    print("outcomes attrs:", [a for a in dir(outs) if not a.startswith("_")])


# ---------- main loop ----------
async def run(args):
    cfg = {"stake": min(args.stake, HARD_MAX_STAKE), "low_cap": min(args.stake, 5.0), "min_edge": args.min_edge,
           "pmin": args.pmin, "pmax": args.pmax}
    live = args.live
    if live and os.environ.get("POLYBOT_CONFIRM_LIVE") != "yes":
        sys.exit("Refusing: live mode needs POLYBOT_CONFIRM_LIVE=yes")
    if args.stake > HARD_MAX_STAKE:
        say(f"stake capped at ${HARD_MAX_STAKE:.0f}")
    client = await make_client() if live else None
    say(f"{'LIVE' if live else 'DRY RUN'} | kinds {args.kinds} | stake ${cfg['stake']} | price {cfg['pmin']}-{cfg['pmax']} | min edge {cfg['min_edge']} | daily loss limit ${args.max_daily_loss}")
    while True:
        st = load_state()
        try:
            settle(st)
            ctx = btc_ctx()
            allowed = True
            try:
                geo = get(GEO)
                if geo.get("blocked"):
                    allowed = False
                    say(f"GEOBLOCK: Polymarket says {geo.get('country')} is restricted. No bets.")
            except Exception as e:
                if live:
                    allowed = False
                    say(f"geoblock check failed ({e}); not trading")
            if os.path.exists(STOP_FILE):
                allowed = False
                say("STOP file present: no new bets")
            if allowed:
                for m in find_markets(args.kinds, ctx):
                    if m["slug"] in st["bets"]:
                        continue
                    books = [get_book(t) for t in m["tokens"]]
                    bet, why = decide(m, ctx, cfg, books)
                    if not bet:
                        continue
                    if risk_used(st) + bet["stake"] > args.max_daily_loss:
                        say(f"daily loss limit reached (${risk_used(st):.2f} at risk/lost); skipping {m['slug']}")
                        continue
                    label = m["outcomes"][bet["k"]]
                    order_id = ""
                    if live:
                        try:
                            order_id = await live_buy(client, m["slug"], label, bet["stake"])
                        except Exception as e:
                            say(f"ORDER FAILED {m['slug']}: {e}")
                            continue
                    st["bets"][m["slug"]] = {"kind": m["kind"], "end": m["end"], "k": bet["k"], "outcome": label, "ask": bet["ask"],
                                              "avg": bet["avg"], "p": bet["p"], "edge": bet["edge"], "stake": bet["stake"],
                                              "shares": bet["shares"], "mode": "live" if live else "dry", "order_id": order_id,
                                              "day": datetime.now(timezone.utc).date().isoformat(), "result": None}
                    say(f"{'BUY' if live else 'WOULD BUY'} {m['kind']} {label} @ {bet['avg']:.3f} (p {bet['p']:.2f}, edge {bet['edge']*100:.1f}c) ${bet['stake']}")
                    log_row([datetime.now().isoformat(timespec="seconds"), "bet", "live" if live else "dry", m["kind"], m["slug"], label,
                             bet["ask"], round(bet["avg"], 4), round(bet["p"], 3), round(bet["edge"], 3), bet["stake"], order_id, "", ""])
            save_state(st)
        except Exception as e:
            say(f"loop error: {e}")
        if args.once:
            return
        await asyncio.sleep(args.interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kinds", default="15m", help="comma list: 5m,15m (15m default: the better-tested desk)")
    ap.add_argument("--stake", type=float, default=5.0)
    ap.add_argument("--max-daily-loss", type=float, default=20.0)
    ap.add_argument("--min-edge", type=float, default=0.04)
    ap.add_argument("--pmin", type=float, default=0.10)
    ap.add_argument("--pmax", type=float, default=0.45)
    ap.add_argument("--interval", type=int, default=20)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--probe", metavar="SLUG")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    args.kinds = [k.strip() for k in args.kinds.split(",") if k.strip() in ("5m", "15m")]
    if args.probe:
        asyncio.run(probe(args.probe))
    else:
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
