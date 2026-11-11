# Data reference

All paths are relative to the project folder. Times are unix seconds unless the column ends in `_ms`.
Prices are in cents (0-100) unless stated. "Maker side" means the resting order that was hit.

## data/lab.db (SQLite)

### trades: every paper and live position
| Column | Meaning |
| --- | --- |
| rule, mode | rule id; `paper` or `live` |
| ticker, coin, grp, close_ts | market, coin, `crypto`/`commodity`, window close time |
| side, px, n, kind | YES/NO, entry price in cents, contracts, `maker`/`taker` |
| t_place, t_fill, secs | when the order was placed and filled; seconds into the window at the fill |
| fee_c | fees in cents (takers, stops) |
| exit | `won`, `lost`, `stopped`, `unresolved`, or NULL while open |
| exit_px, t_exit, pnl_c | stop price, exit time, result in cents for the whole position (divide by n) |
| result | market result `yes`/`no` |
| feat | JSON snapshot at the fill: secs, spread, bid_size, ask_size, imb (our side's share of top-of-book size), flow_sell_5s / flow_buy_5s (contracts traded to/against our side in the last 5 s), and for crypto index, strike, dist_bp, index_age, settle_avg, settle_n |
| restart | 1 if the program restarted while the position was open (added Nov 11). It is held to settlement and counted; `tools/analyze.py` also shows results without these |

### orders: every order that ended without filling
`rule, mode, ticker, coin, close_ts, side, px, n, t_place, t_end, secs, outcome (cancelled/expired), queue
(size ahead when placed), at_px_vol (contracts that traded at our price while we rested), won (1 if our side
went on to win, set at settlement)`. Compare `won` here with the trades' win rate: unfilled bids winning more
often than filled ones means fills are adversely selected.

### markets
`ticker, coin, grp, open_ts, close_ts, strike, result`.

### events
`t, level (info/warn/error/halt/trade), msg`. Errors include WebSocket problems and REST-audit mismatches.

```sql
-- results per rule, cents per contract
SELECT rule, mode, COUNT(*) n, ROUND(AVG(pnl_c / n), 3) mean_c, SUM(pnl_c <= 0) bad
FROM trades WHERE exit IS NOT NULL GROUP BY rule, mode;
-- filled vs unfilled win rates
SELECT rule, AVG(won) unfilled_won, COUNT(*) FROM orders WHERE won IS NOT NULL GROUP BY rule;
```
`tools/analyze.py` runs these and more with slot-clustered error bars.

## data/rec/YYYY-MM-DD/ (recorded market data)

Hourly files in UTC (`kind_YYYY-MM-DD_HH.csv`), gzipped when the hour ends. Every row starts with `recv_ms`,
the time this machine received it, on its own clock: compare with the exchange's own stamps to measure
latency. Replays must order events by `recv_ms`.

| File | Columns |
| --- | --- |
| `top_*.csv` | recv_ms, exch_ms, ticker, yes_bid, yes_ask, bid_size, ask_size: one row per top-of-book change. An empty side is a blank price with size 0. **Before the Nov 11 fix, one-sided states were not written at all**, so in those files a price can look stale while that side was actually empty (mostly the final minutes) |
| `trades_*.csv` | recv_ms, ts_ms (exchange), ticker, yes_price, count, taker_side (`no` = maker bought YES) |
| `index_*.csv` | recv_ms, index_id (BRTI = BTC, ETHUSD_RTI, ...), value, src_ms, avg60, avg60_n, settle_avg, settle_n (final minute only: the running settlement average and its tick count) |
| `pyth_*.csv` | recv_ms, underlying (e.g. Metal.XAU/USD), value, src_ms: at most one row per second each |
| `markets_*.csv` | recv_ms, ticker, coin, open_ts, close_ts, strike, result (a second row when the result arrives) |
| `raw_*.jsonl` | only with `"record": {"mode": "full"}`: `[recv_time, <exact WebSocket message>]` per line |

```python
import pandas as pd, glob
top = pd.concat(pd.read_csv(f) for f in sorted(glob.glob("data/rec/*/top_*.csv*")))
```

## reports/

`YYYY-MM-DD.md` and `latest.md`: the daily scorecard, written just after midnight ET and from the dashboard.

## Older data (from the Late Maker bot, on Whit's Windows PC)

`book_<COIN>_<date>.csv` (1-second REST snapshots, stamped by a clock 1.2 s slow), `results.csv`, and
`trades*.csv` (Kalshi trade history). `tools/replay_late_maker.py <folder>` replays them through this engine
with the clock offset corrected. Not on this server unless copied.
