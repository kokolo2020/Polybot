#!/usr/bin/env python3
"""
Polybot SERVER - Polymarket PAPER trading bot that runs 24/7 with no browser.
Standard library only (Python 3.8+). No wallet, no real orders.

Features (same as the web dashboard v2.3):
  - Real Polymarket markets + order books (Gamma + CLOB), fills simulated by walking the book
  - Fractional-Kelly sizing, take-profit / stop-loss / edge-gone exits
  - BTC 15m + 1h Up/Down markets priced from Binance spot
  - Telegram alerts on every BUY/SELL + a recap every 8h
  - state.json (atomic saves) and a tiny HTTP endpoint: /state.json and /health

Run:
  export TG_TOKEN="123:ABC"   # optional
  export TG_CHAT="123456789"  # optional
  python3 polybot_server.py                 # scan every 60s forever
  python3 polybot_server.py --once          # single scan (test)
  python3 polybot_server.py --status        # print equity / positions
  python3 polybot_server.py --interval 45 --port 8787 --preset aggressive

Paper only. The order-book signal is a placeholder, NOT a proven edge.
"""
import argparse, calendar, json, math, os, sys, threading, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
BIN = "https://data-api.binance.vision"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.environ.get("POLYBOT_STATE", os.path.join(HERE, "state.json"))

PRESETS = {
    "cautious":   dict(min_edge=0.02, imbalance_weight=0.04, kelly_fraction=0.25, max_pos_pct=0.05, max_open=12, markets_per_scan=40, take_profit=0.05, stop_loss=0.04),
    "normal":     dict(min_edge=0.02, imbalance_weight=0.04, kelly_fraction=0.25, max_pos_pct=0.05, max_open=12, markets_per_scan=40, take_profit=0.05, stop_loss=0.04),
    "aggressive": dict(min_edge=0.01, imbalance_weight=0.08, kelly_fraction=0.50, max_pos_pct=0.10, max_open=50, markets_per_scan=80, take_profit=0.05, stop_loss=0.05),
}
CFG = dict(start_bank=1000.0, min_liquidity=5000.0, min_price=0.08, max_price=0.92, fee_rate=0.0,
           book_levels=5, max_hours=240, btc_on=True, recap_hours=8, **PRESETS["aggressive"])

LOCK = threading.Lock()
BTC = {"ctx": None}


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- http
def get_json(url, params=None, tries=3):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "polybot-paper/3.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            err = e
            if e.code != 429 and e.code < 500:
                raise
        except Exception as e:
            err = e
        time.sleep(1.5 * (i + 1))
    raise err


def parse_list(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return []
    return v or []


# ---------------------------------------------------------------- telegram
def tg(text):
    tok, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if not tok or not chat:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=data), timeout=15).read()
    except Exception as e:
        log("telegram error:", type(e).__name__)  # never print the token/URL


# ---------------------------------------------------------------- state
def new_state():
    return dict(cash=CFG["start_bank"], positions={}, realized=0.0, scans=0, trades=[], curve=[],
                stats=dict(w=0, l=0, best=0.0, worst=0.0), last_recap=time.time(), started=time.time(), btc=None)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            st = json.load(f)
        base = new_state()
        base.update(st)
        return base
    return new_state()


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


def equity(st, marks=None):
    marks = marks or {}
    return st["cash"] + sum(p["shares"] * marks.get(t, p.get("last", p["entry"])) for t, p in st["positions"].items())


def record(st, a, q, o, px, sh, usd, pnl=None, note=""):
    st["trades"].append(dict(t=time.strftime("%Y-%m-%d %H:%M:%S"), a=a, q=q, o=o, px=round(px, 4), sh=round(sh, 2),
                             usd=round(usd, 2), pnl=None if pnl is None else round(pnl, 2), note=note))
    st["trades"] = st["trades"][-500:]
    if pnl is not None:
        s = st["stats"]
        s["w" if pnl > 0 else "l"] += 1
        s["best"], s["worst"] = max(s["best"], pnl), min(s["worst"], pnl)
    emoji = "\U0001F7E2 BUY" if a == "BUY" else "\U0001F534 SELL"
    tg(f"{emoji} {o} {sh:.0f} @ {px:.3f}" + (f"  P&L {pnl:+.2f}" if pnl is not None else "") + f"\n{q[:90]}\n{note}")


# ---------------------------------------------------------------- books / signal
def fetch_book(tok):
    b = get_json(f"{CLOB}/book", {"token_id": tok})
    bids = sorted(((float(x["price"]), float(x["size"])) for x in b.get("bids", [])), reverse=True)
    asks = sorted((float(x["price"]), float(x["size"])) for x in b.get("asks", []))
    return bids, asks


def walk_buy(asks, usd):
    shares = cost = 0.0
    for p, s in asks:
        take = min(usd - cost, p * s)
        if take <= 0:
            break
        shares += take / p
        cost += take
    return shares, cost, (cost / shares if shares else 0.0)


def walk_sell(bids, shares):
    sold = proceeds = 0.0
    for p, s in bids:
        x = min(shares - sold, s)
        if x <= 0:
            break
        sold += x
        proceeds += x * p
    return sold, proceeds


def signal(bids, asks):
    n = CFG["book_levels"]
    bd = sum(p * s for p, s in bids[:n])
    ad = sum(p * s for p, s in asks[:n])
    if bd + ad == 0:
        return None
    imb = (bd - ad) / (bd + ad)
    mid = (bids[0][0] + asks[0][0]) / 2
    return min(0.99, max(0.01, mid + CFG["imbalance_weight"] * imb))


# ---------------------------------------------------------------- BTC model
def Phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def fetch_btc_ctx():
    k = get_json(BIN + "/api/v3/klines", {"symbol": "BTCUSDT", "interval": "1m", "limit": 120})
    opn = {c[0]: float(c[1]) for c in k}
    cls = {c[0]: float(c[4]) for c in k}
    cl = [float(c[4]) for c in k][-61:]
    rets = [math.log(cl[i] / cl[i - 1]) for i in range(1, len(cl))]
    mean = sum(rets) / len(rets)
    sig = max(0.0002, math.sqrt(sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)))
    return dict(S=cl[-1], sig=sig, open=opn, close=cls)


def btc_p_up(K, end_ms):
    c = BTC["ctx"]
    tau = max(0.1, (end_ms - time.time() * 1000) / 60000)
    return min(0.98, max(0.02, Phi(math.log(c["S"] / K) / (c["sig"] * math.sqrt(tau)))))


def et_slug(t_ms, with_year):
    # US Eastern: EDT (UTC-4) from 2nd Sunday of March 2am to 1st Sunday of Nov 2am, else EST (UTC-5)
    t = t_ms / 1000
    y = time.gmtime(t).tm_year
    def sundays(month):
        return [w[6] for w in calendar.monthcalendar(y, month) if w[6]]
    dst_start = calendar.timegm((y, 3, sundays(3)[1], 7, 0, 0))   # 2am EST = 07:00 UTC
    dst_end = calendar.timegm((y, 11, sundays(11)[0], 6, 0, 0))   # 2am EDT = 06:00 UTC
    off = -4 if dst_start <= t < dst_end else -5
    e = time.gmtime(t + off * 3600)
    months = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
    h = e.tm_hour % 12 or 12
    ap = "am" if e.tm_hour < 12 else "pm"
    return f"bitcoin-up-or-down-{months[e.tm_mon - 1]}-{e.tm_mday}-{str(e.tm_year) + '-' if with_year else ''}{h}{ap}-et"


def event_by_slug(slug):
    try:
        r = get_json(f"{GAMMA}/events", {"slug": slug}, tries=2)
        return r[0] if isinstance(r, list) and r else None
    except Exception:
        return None


def fetch_btc_markets():
    now = time.time() * 1000
    jobs = []
    b15 = int(now / 900000) * 900
    for i in (0, 1):
        jobs.append(("15m", 900e3, [f"btc-updown-15m-{b15 + i * 900}"]))
    for i in (0, 1):
        t = int((now + i * 3600e3) // 3600e3) * 3600e3
        jobs.append(("1h", 3600e3, [et_slug(t, True), et_slug(t, False)]))
    out = []
    for kind, dur, slugs in jobs:
        ev = None
        for sl in slugs:
            ev = event_by_slug(sl)
            if ev and ev.get("markets"):
                break
            ev = None
        if not ev:
            continue
        m = ev["markets"][0]
        toks, outs = parse_list(m.get("clobTokenIds")), parse_list(m.get("outcomes"))
        if len(toks) != 2 or len(outs) != 2 or m.get("closed"):
            continue
        try:
            end = time.mktime(time.strptime((m.get("endDate") or ev.get("endDate"))[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        except Exception:
            continue
        end_ms = end * 1000
        if end_ms < now + 60e3:
            continue
        start = end_ms - dur
        K = BTC["ctx"]["open"].get(int(start // 60000) * 60000)
        if not K:
            continue
        up = next((i for i, o in enumerate(outs) if str(o).lower() == "up"), 0)
        out.append(dict(q=(m.get("question") or ev.get("title") or kind)[:90], tokens=toks, outcomes=outs,
                        liq=float(m.get("liquidity") or 0), end=end_ms, btc=dict(kind=kind, K=K, up=up)))
    return out


# ---------------------------------------------------------------- markets
def fetch_markets():
    data = get_json(f"{GAMMA}/markets", dict(active="true", closed="false", order="volume24hr", ascending="false",
                                              limit=300 if CFG["max_hours"] > 0 else CFG["markets_per_scan"]))
    out = []
    for m in data:
        toks, outs = parse_list(m.get("clobTokenIds")), parse_list(m.get("outcomes"))
        if len(toks) != 2 or len(outs) != 2 or float(m.get("liquidity") or 0) < CFG["min_liquidity"]:
            continue
        end = None
        raw = m.get("endDate") or m.get("endDateIso")
        if raw:
            try:
                end = (time.mktime(time.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone) * 1000
            except Exception:
                pass
        out.append(dict(q=m.get("question", "?"), tokens=toks, outcomes=outs, liq=float(m.get("liquidity") or 0), end=end))
    if CFG["max_hours"] > 0:
        t, lim = time.time() * 1000, CFG["max_hours"] * 3600e3
        out = sorted((m for m in out if m["end"] and t < m["end"] <= t + lim), key=lambda m: m["end"])[:CFG["markets_per_scan"]]
    return out


# ---------------------------------------------------------------- scan
def scan(st):
    marks, now = {}, time.strftime("%Y-%m-%d %H:%M:%S")
    markets = fetch_markets()
    BTC["ctx"] = None
    if CFG["btc_on"] or any(p.get("btc") for p in st["positions"].values()):
        try:
            BTC["ctx"] = fetch_btc_ctx()
        except Exception as e:
            log("BTC feed error:", e)
    if CFG["btc_on"] and BTC["ctx"]:
        try:
            bm = fetch_btc_markets()
            markets = bm + markets
            st["btc"] = dict(S=BTC["ctx"]["S"], n=len(bm))
            log(f"BTC ${BTC['ctx']['S']:.0f} vol {BTC['ctx']['sig'] * 100:.3f}%/min, {len(bm)} up/down markets")
        except Exception as e:
            log("BTC markets error:", e)

    pool = ThreadPoolExecutor(max_workers=8)

    def safe_book(tok):
        try:
            return fetch_book(tok)
        except Exception as e:
            log("book error:", str(e)[:80])
            return None

    # settle expired BTC windows (approximation from Binance closes; real resolution is Chainlink)
    for tok, pos in list(st["positions"].items()):
        b = pos.get("btc")
        if not b or not BTC["ctx"] or time.time() * 1000 < b["end"] + 15000:
            continue
        end_px = BTC["ctx"]["close"].get(int((b["end"] - 60000) // 60000) * 60000)
        if not end_px:
            continue
        win = (end_px > b["K"]) == b["up"]
        payout = pos["shares"] if win else 0.0
        pnl = payout - pos["shares"] * pos["entry"]
        st["cash"] += payout
        st["realized"] += pnl
        record(st, "SELL", pos["q"], pos["outcome"], 1.0 if win else 0.0, pos["shares"], payout, pnl,
               f"settled {'WIN' if win else 'LOSS'} (BTC {b['K']:.0f} -> {end_px:.0f})")
        del st["positions"][tok]

    # exits
    books = dict(zip(st["positions"].keys(), pool.map(safe_book, list(st["positions"].keys()))))
    for tok, pos in list(st["positions"].items()):
        bk = books.get(tok)
        if not bk or not bk[0]:
            continue
        bids, asks = bk
        bid = bids[0][0]
        marks[tok] = pos["last"] = bid
        p = None
        if asks:
            if pos.get("btc") and BTC["ctx"]:
                pu = btc_p_up(pos["btc"]["K"], pos["btc"]["end"])
                p = pu if pos["btc"]["up"] else 1 - pu
            else:
                p = signal(bids, asks)
        reason = None
        if bid - pos["entry"] >= CFG["take_profit"]:
            reason = "take_profit"
        elif pos["entry"] - bid >= CFG["stop_loss"]:
            reason = "stop_loss"
        elif p is not None and p < bid - 0.01:
            reason = "edge_gone"
        if reason:
            sold, proceeds = walk_sell(bids, pos["shares"])
            proceeds *= 1 - CFG["fee_rate"]
            if sold <= 0:
                continue
            pnl = proceeds - sold * pos["entry"]
            st["cash"] += proceeds
            st["realized"] += pnl
            pos["shares"] -= sold
            record(st, "SELL", pos["q"], pos["outcome"], proceeds / sold, sold, proceeds, pnl, reason)
            if pos["shares"] < 0.01:
                del st["positions"][tok]

    # entries
    eq = equity(st, marks)
    held_toks = set(st["positions"])
    want = [t for m in markets for t in m["tokens"] if t not in books]
    fetched = dict(zip(want, pool.map(safe_book, want)))
    fetched.update(books)
    priced = trig = 0
    for m in markets:
        if any(t in st["positions"] for t in m["tokens"]) or len(st["positions"]) >= CFG["max_open"]:
            continue
        best = None
        for k in (0, 1):
            tok, bk = m["tokens"][k], fetched.get(m["tokens"][k])
            if not bk or not bk[0] or not bk[1]:
                continue
            bids, asks = bk
            ask = asks[0][0]
            if not CFG["min_price"] <= ask <= CFG["max_price"]:
                continue
            p = signal(bids, asks)
            if p is None:
                continue
            if m.get("btc"):
                if m["end"] - time.time() * 1000 < 90e3:
                    continue
                pu = btc_p_up(m["btc"]["K"], m["end"])
                p = pu if k == m["btc"]["up"] else 1 - pu
            priced += 1
            edge = p - ask - ask * CFG["fee_rate"]
            if edge >= CFG["min_edge"]:
                trig += 1
                if not best or edge > best["edge"]:
                    best = dict(tok=tok, o=m["outcomes"][k], ask=ask, p=p, edge=edge, asks=asks, bids=bids)
        if not best:
            continue
        kelly = best["edge"] / (1 - best["ask"])
        usd = min(eq * CFG["kelly_fraction"] * kelly, eq * CFG["max_pos_pct"], st["cash"])
        if usd < 5:
            continue
        shares, cost, avg = walk_buy(best["asks"], usd)
        total = cost * (1 + CFG["fee_rate"])
        if shares <= 0 or total > st["cash"]:
            continue
        st["cash"] -= total
        pos = dict(q=m["q"], outcome=best["o"], shares=shares, entry=avg, opened=now, last=best["bids"][0][0], end=m["end"])
        if m.get("btc"):
            pos["btc"] = dict(K=m["btc"]["K"], end=m["end"], up=best["o"] == m["outcomes"][m["btc"]["up"]])
        st["positions"][best["tok"]] = pos
        record(st, "BUY", m["q"], best["o"], avg, shares, total, None, f"p={best['p']:.3f} edge={best['edge']:.3f}")
    pool.shutdown()

    st["scans"] += 1
    eq = equity(st, marks)
    st["curve"].append([int(time.time()), round(eq, 2)])
    st["curve"] = st["curve"][-3000:]
    log(f"scan #{st['scans']} equity ${eq:,.2f} cash ${st['cash']:,.2f} open {len(st['positions'])} "
        f"realized ${st['realized']:+.2f} | markets {len(markets)} priced {priced} triggers {trig}")


def recap_text(st):
    eq = equity(st)
    s = st["stats"]
    n = s["w"] + s["l"]
    ups = sum((p.get("last", p["entry"]) - p["entry"]) * p["shares"] for p in st["positions"].values())
    return (f"POLYBOT recap (paper)\nEquity ${eq:,.2f} ({eq - CFG['start_bank']:+.2f}, {(eq / CFG['start_bank'] - 1) * 100:+.2f}%)\n"
            f"Cash ${st['cash']:,.2f} | open {len(st['positions'])} | unrealized {ups:+.2f}\n"
            f"Realized {st['realized']:+.2f} | win rate {round(s['w'] / n * 100) if n else 0}% ({s['w']}/{n})\n"
            f"Scans {st['scans']} | best {s['best']:+.2f} worst {s['worst']:+.2f}")


# ---------------------------------------------------------------- http endpoint
class H(BaseHTTPRequestHandler):
    state = None

    def do_GET(self):
        with LOCK:
            body = json.dumps(dict(self.state, equity=round(equity(self.state), 2), cfg=CFG)).encode() if self.path.startswith("/state") else b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "application/json" if self.path.startswith("/state") else "text/plain")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--port", type=int, default=0, help="serve /state.json on this port (0 = off)")
    ap.add_argument("--preset", choices=list(PRESETS), default="aggressive")
    ap.add_argument("--max-hours", type=float)
    ap.add_argument("--no-btc", action="store_true")
    a = ap.parse_args()
    CFG.update(PRESETS[a.preset])
    if a.max_hours is not None:
        CFG["max_hours"] = a.max_hours
    if a.no_btc:
        CFG["btc_on"] = False
    st = load_state()
    if a.status:
        print(recap_text(st))
        for p in st["positions"].values():
            print(f"  {p['outcome']:>5} {p['shares']:8.1f} @ {p['entry']:.3f} -> {p.get('last', p['entry']):.3f} | {p['q'][:60]}")
        return
    if a.port:
        H.state = st
        threading.Thread(target=lambda: ThreadingHTTPServer(("0.0.0.0", a.port), H).serve_forever(), daemon=True).start()
        log(f"serving /state.json on :{a.port}")
    log(f"Polybot server start | preset {a.preset} | every {a.interval}s | telegram {'on' if os.environ.get('TG_TOKEN') else 'off'}")
    tg("Polybot server started (paper). Alerts on every trade + recap every 8h.")
    while True:
        try:
            with LOCK:
                scan(st)
                if time.time() - st["last_recap"] >= CFG["recap_hours"] * 3600:
                    tg(recap_text(st))
                    st["last_recap"] = time.time()
                save_state(st)
        except Exception as e:
            log("scan error:", type(e).__name__, str(e)[:120])
        if a.once:
            break
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
