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
