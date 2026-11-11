# Kalshi Maker Lab: instructions for Claude Code

You are working on Whit's Kalshi trading research program, running on his Linux server. He is an EE student
trading small size who wants the truth about whether an edge exists, not reassurance. Lead with the answer,
put numbers on everything, say how sure you are, and name what data would settle what you can't.

Read before doing anything else: `README.md`, `docs/HISTORY.md` (what was learned and how), `docs/DATA.md`
(every data file and how to query it), `docs/REVIEW_LOOP.md` (the daily review protocol), `docs/DESIGN.md`.

## Hard rules

1. **Never turn live trading on, tick "Trade live" on a rule, change position sizes, or touch `keys.json` or
   `secrets/`** unless Whit asks for that specific change in this conversation. Read-only analysis is always fine.
2. **Never tune a frozen rule on the data that scores it.** To try a variation, add a new rule with a new id
   and let both run. Change at most one thing at a time, log it in `docs/CHANGES.md` with the result that
   would count as success, and judge it only on data recorded afterwards.
3. **Use arrival timestamps.** Every recorded row has `recv_ms` (when this machine received it). A backtest that
   lets a decision see data that arrived later is wrong. This exact bug inflated the old bot's results 2x.
4. **Never drop a market or a day because it lost.** That manufactured +0.18c of false edge before.
5. **Report uncertainty properly.** Trades in the same 15-minute slot are correlated: cluster standard errors
   by slot (`kml/store.py: stats` does this). Wins are 1-3c and losses 50-99c, so a run of wins proves little.
6. Don't stop or restart the service unless asked (`sudo systemctl restart kalshi-maker-lab`). Running the
   tests and `tools/analyze.py` is safe while it runs.
7. Commit code changes with clear messages; don't commit `data/`, `reports/`, `config.json`, `keys.json`,
   `secrets/` (they are gitignored; keep it that way).

## Useful commands

```bash
.venv/bin/python tools/analyze.py                 # standard health and results report (read-only)
.venv/bin/python tools/analyze.py --days 3        # last three days only
.venv/bin/python -m unittest discover -s tests    # 26 tests, no network
.venv/bin/python tools/smoke_test.py              # whole program against a fake exchange, ~45 s
.venv/bin/python -m kml.scorecard                 # rewrite reports/latest.md
journalctl -u kalshi-maker-lab -n 200 --no-pager  # service log
sqlite3 data/lab.db                               # trades, orders, markets, events (see docs/DATA.md)
```

## Code map

`kml/engine.py` decisions, paper fills, stops, settlement (no network) · `kml/book.py` order book ·
`kml/ws.py` WebSocket client · `kml/app.py` wiring, window roll-over, results, REST audit ·
`kml/live.py` real orders · `kml/kalshi_rest.py` REST client (carried over from the old bot, which traded real
money with it) · `kml/config.py` frozen rules and defaults · `kml/store.py` SQLite and statistics ·
`kml/recorder.py` data files · `kml/web.py` + `kml/static/` dashboard · `kml/keys.py` API key page.
