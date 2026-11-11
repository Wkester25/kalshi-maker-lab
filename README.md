# Kalshi Maker Lab

One program for Kalshi's 15-minute markets (7 crypto, 8 commodity). It records market data at WebSocket
speed, runs a set of frozen trading rules against the live market in **paper mode**, keeps score, and
shows everything on a web page behind a login. It can place real orders, but that is switched off and locked by default.

It was built after an independent review of the "Late Maker" bot (Nov 2026). The short version of that review:
the old backtests were inflated by a clock that ran 1.2 s slow, no rule is proven yet, and the way to find out
is to score a small set of fixed rules on fresh data. This program does that scoring continuously.

## What is and is not verified

| Part | Status |
| --- | --- |
| Trading engine and paper fill model | Replayed over 3,031 recorded windows; matches the review's separately written simulator on all five rules |
| Order book, rules, stop, settlement, stats, login, key page | 21 unit tests (`python -m unittest discover -s tests`) |
| WebSocket client, window roll-over, recorder, web page, login | Run end to end against a fake exchange (`python tools/smoke_test.py`); login flow checked in a real browser |
| WebSocket message formats | Written from Kalshi's published docs. **Not yet run against the real exchange.** The program checks its WebSocket book against the REST book every minute and warns on the page if they disagree |
| REST client (signing, orders, cancels) | Carried over unchanged from the Late Maker bot, which traded real money with it |
| Live order logic in this program | Follows the Late Maker bot's sequences. **Has not traded on a live account.** First use: one rule, one contract |

## Quick start

Python 3.10 or newer.

**Windows:** double-click `Start Maker Lab.bat`. The first run sets up a private environment.

**Linux server:**

```bash
git clone <your repo url> kalshi-maker-lab && cd kalshi-maker-lab
./deploy/install.sh          # sets up .venv, runs the self-tests, prints the service commands
```

**API keys.** Easiest: sign in to the web page and open **API keys**. Enter the key ID Kalshi gave you, then
upload the private key file, paste its contents, or give the path of a file already on the server. You confirm
with your web password. The page checks the key can sign, stores it in `secrets/` (readable only by the
program's user), and "Test with Kalshi" checks Kalshi accepts it and shows your balance and the round-trip
time. A new key takes effect at once; if the program was running without keys, it offers a restart to switch
to the WebSocket feed (the restart button works when it runs as a service).

The key file itself never goes back to the browser, and `secrets/` and `keys.json` are never committed. You
can still set keys by hand: copy `keys.example.json` to `keys.json` (same shape as the old `lm_keys.json`).
Without keys the program runs on public REST polling: slower, and without the settlement-index feed.

**Web page and login.** `./deploy/install.sh` asks for a username and password and whether the page should be
reachable on your local network. To change either later:

```bash
.venv/bin/python run.py --setup            # username, password, local network yes/no
.venv/bin/python run.py --set-password     # password only
sudo systemctl restart kalshi-maker-lab
```

With local-network access on, open `http://<server-address>:8787` from any device at home and sign in. The
address is printed when the program starts. Details:

- The password is stored only as a salted hash in `config.json`. Five wrong attempts lock that device out for
  five minutes. A sign-in lasts 30 days.
- If you never run `--setup`, the program makes a random password on first start and prints it once.
- The page is plain HTTP. That is fine on a home network. **Do not forward port 8787 on your router**; from
  outside the house use a VPN or `ssh -L 8787:127.0.0.1:8787 you@server` instead.
- If the server runs a firewall: `sudo ufw allow from 192.168.0.0/16 to any port 8787 proto tcp`.
- Scripts can call the API with the header `X-Token: <web.token from config.json>` instead of signing in.

This page can change trading settings, so pick a real password.

## Live trading

Live trading is a switch at the top of the dashboard, off by default. Real orders need both:

1. The **Live trading** switch on (it asks for your web password, and needs an API key loaded).
2. **Trade live** ticked on the rule (Edit on the rule's row).

Everything else keeps trading on paper alongside. Switching on takes effect from the next 15-minute window.
Switching off cancels resting live orders at once and holds any open positions to settlement. **Stop all**
also sends Kalshi a cancel-all for the account. The switch is remembered across restarts. Taker rules are paper only in this version. Limits that
apply to live trading: a cap on money at risk at once (10% of the stated bankroll), a daily loss limit, a kill
switch per rule (down $6 per contract), and a halt if a cancel cannot be confirmed.

## The rules

These are frozen. They are scored on data that did not choose them; change one and its test starts again.

| Rule | What it does | Evidence when frozen |
| --- | --- | --- |
| `crypto_99` | Bid on the crypto favourite at 99c and above, 10:00-12:00 and the last 30 s, hold to settlement | 569 simulated trades, no losses, 2.5 days. One loss erases about 130 wins, so this is not proof |
| `commodity_mid` | Bid on the commodity favourite at 75-90c, minutes 2-12, hold | Tape: makers on this side earned +5.7c. A slow bot simulated about +1.9c, range includes zero |
| `commodity_take98` | Buy the commodity favourite at the ask at 98-99.5c, minutes 4-9 (10 contracts) | +1.26c on 203 trades. Paper only |
| `commodity_early` | The Late Maker commodity rule: 96-99.5c, 9:00-13:00, 50c stop | +0.8c pooled, +0.2c on newer windows |
| `crypto_early` | The Late Maker crypto rule, kept as a control | No support: -0.3c on newer windows |

## What paper mode assumes about fills

- An order is live 150 ms after the decision; a cancel takes 150 ms. A bid can be hit in between.
- A trade through our price always fills us.
- A trade at our price fills us only after the size displayed ahead of us has traded.
- The market's bid is read with our own orders removed, so the program never raises against itself.

Every bid that did not fill is logged with whether its side went on to win. If unfilled bids win clearly more
often than filled ones, fills are being selected against the rule.

## What gets recorded

Under `data/rec/<date>/`, hourly files, gzipped when the hour ends, every row stamped with its arrival time:
top of book on every change, every trade, CF Benchmarks index values (including the running 60-second
settlement average in the final minute), Pyth values, and each window's strike and result. Set
`"record": {"mode": "full"}` in `config.json` to keep every raw WebSocket message instead (large).

The index feed is the settlement source for crypto. The Late Maker bot never had it; its exchange-average
price lagged Kalshi by 1-8 s.

## Daily scorecard and review

`reports/latest.md` is rewritten just after midnight ET and on demand from the page. It is the input to the
daily review described in `docs/REVIEW_LOOP.md`: read the scorecard, change at most one thing, write it down,
judge it only on later data. `docs/DESIGN.md` explains the design and what the review found.

## Files

```
run.py                     start here
kml/engine.py              rules, paper execution, stops, settlement (no network code)
kml/book.py                order book from snapshots and deltas
kml/ws.py                  WebSocket client
kml/kalshi_rest.py         REST client (carried over from the Late Maker bot)
kml/live.py                real orders (locked off by default)
kml/app.py                 wiring: feeds, window roll-over, results, recorder
kml/web.py, kml/static/    web page, login, API keys page
kml/keys.py                API key storage and checks       -> keys.json, secrets/
kml/store.py               SQLite store and statistics       -> data/lab.db
kml/recorder.py            market data to disk               -> data/rec/
kml/scorecard.py           daily report                      -> reports/
tools/smoke_test.py        whole program against a fake exchange
tools/replay_late_maker.py score the rules on the old Late Maker recordings
deploy/                    Linux service files
```

Settings that differ from the defaults go in `config.json` (see `config.example.json`). `config.json`,
`keys.json`, `data/` and `reports/` are never committed.
