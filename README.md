# Polybot Desk

Polymarket **paper-trading** dashboard (real prices, fake money). Single-file HTML app.

- Scans live Polymarket markets + order books, simulates fills, fractional-Kelly sizing.
- v2.3: BTC 15m + 1h Up/Down markets priced from Binance spot.
- Deploy: Netlify, publish directory = repo root. `_redirects` proxies `/gamma`, `/clob`, `/binance`.
- `python/polybot.py`: original CLI version.

Paper only. The signal is not a proven edge.

## 24/7 server (no browser)
`python/polybot_server.py` — stdlib only, runs on any VPS.

    sudo mkdir -p /opt/polybot && sudo cp python/polybot_server.py /opt/polybot/
    python3 /opt/polybot/polybot_server.py --once        # test one scan
    # edit python/polybot.service (token + chat id), then:
    sudo cp python/polybot.service /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now polybot
    journalctl -u polybot -f                              # live log

State: `/opt/polybot/state.json`. Optional `/state.json` on port 8787.

## Live test (friends, own accounts) - `python/polybot_live.py`
For people **outside** restricted countries only, on their **own** Polymarket account with their **own** money.
It checks Polymarket's geoblock endpoint and refuses to trade if blocked. Never share a private key with anyone.
The paper edge is NOT proven. Treat any real money as money you can lose.

1. **Dry run first (no keys, no money), 3+ days:** `python3 python/polybot_live.py --kinds 15m,5m`
   Logs what it WOULD buy and, later, Polymarket's real result for each bet (`python/live_log.csv`).
2. **Live, tiny:** `pip install polymarket-client` (package name per Polymarket's docs), then set
   `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_WALLET_ADDRESS`, `POLYBOT_CONFIRM_LIVE=yes`; fund $20-50 only (use a session key if you can).
3. **Before the first live bet:** `python3 python/polybot_live.py --probe <market-slug>` and send the output. The docs only show yes/no
   markets, so the Up/Down asset mapping is unverified until this runs once.
4. `python3 python/polybot_live.py --live --stake 5 --max-daily-loss 20 --kinds 15m`
5. Stop anytime: create a file named `STOP` next to the script. Hard cap: $25 per bet.

## Data collector - `python/collect.py` (read-only, no keys, places no bets)
Logs every BTC 5m/15m window every ~30s (Polymarket prices + book top, Binance spot/strike/volatility, time left)
and Polymarket's real result afterwards, to `python/data/`. Betting only teaches us about the bets we picked;
this builds an unbiased dataset to test ideas out-of-sample. Also saves any price-like fields Polymarket
exposes to `meta.jsonl`. It also logs Polymarket's own start price ("price to beat") and close price per window
(`pm_open`, `pm_close`, from polymarket.com/api/crypto/crypto-price; variant `fiveminute` for 5m, `fifteen` for 15m).
Needs only Python 3 (no installs). Reading data works from Thailand; it never trades.

    git clone https://github.com/kokolo2020/Polybot && cd Polybot
    python3 python/collect.py          # leave running; Ctrl-C to stop, safe to restart
