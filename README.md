# Polybot Desk

Polymarket **paper-trading** dashboard (real prices, fake money). Single-file HTML app.

- Scans live Polymarket markets + order books, simulates fills, fractional-Kelly sizing.
- v2.3: BTC 15m + 1h Up/Down markets priced from Binance spot.
- Deploy: Netlify, publish directory = repo root. `_redirects` proxies `/gamma`, `/clob`, `/binance`.
- `python/polybot.py`: original CLI version.

Paper only. The signal is not a proven edge.
