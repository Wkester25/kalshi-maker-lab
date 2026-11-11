# Design notes

## What the review found (Nov 2026)

1. **The old backtests looked ahead.** The recording PC's clock ran about 1.2 s behind Kalshi's trade
   timestamps, so each recorded order book showed the market 1.2 s after its timestamp. The simulated bot
   cancelled bids using information it could not have had. The claimed +1.3c per trade became about +0.6c
   with the clocks aligned, and the range included zero.
2. **The old bot bid against itself.** It read the public book, which contained its own order, and raised
   one tick at a time. While its order was the best bid it also could not see the market fall beneath it.
3. **The stop depended on price feeds it did not use.** A feed outage skipped the stop.
4. **Fills are selected against a slow maker.** Bids that saw a print at their price but did not fill won
   99%; bids that filled won 95.8%.
5. **Crypto pays nobody passively.** Across 644M contracts, makers as a class made -0.04c per contract.
   Commodity makers made +0.38c, and +5.7c when buying the favourite at 55-90c in minutes 2-12. That money
   comes from makers quoting the underdog side, who are picked off by faster traders.
6. **The Reddit trader's numbers imply entries near 99.4c**, not 94-98c. At 99c and above the losing stretch
   is 12:00-14:30; 10:00-12:00 and the last 30 s showed no losses in 569 simulated trades.
7. **Kalshi streams the settlement index.** The `cfbenchmarks_value` WebSocket channel carries the CF
   Benchmarks index for each coin and, in the final minute, the running 60-second average the market settles
   on. `pyth_value` does the same for commodities.

## How the program answers each one

| Finding | In this program |
| --- | --- |
| Slow clock | The exchange stamps each WebSocket message; the largest (stamp - arrival) over the last two minutes estimates the clock offset, and all window timing uses the corrected time. The offset is shown on the page |
| Look-ahead | Every recorded row carries its arrival time, so a replay can only use what had arrived |
| Self-bidding | `Book.quote(side, own)` removes our resting size before reading the best bid |
| Stop tied to feeds | Rules use only Kalshi's own book. The stop reads the book and nothing else |
| Fill selection | Paper fills use latency and queue position; unfilled bids are logged with their eventual outcome |
| Where the edge might be | Five frozen rules run side by side on paper, each scored with slot-clustered error bars |
| Settlement index | Recorded for every crypto market, shown on the page, and saved with each fill for later modelling |

## Structure

The engine (`kml/engine.py`) does no network I/O. Market data is pushed in, the clock is injected, and live
orders go through an executor. The same engine is driven by the WebSocket, by REST polling, or by a replay.
That is how it was checked: the Nov 8-10 recordings were replayed through it and compared with the review's
separately written simulator.

| Rule | Engine replay | Review simulator |
| --- | --- | --- |
| `crypto_99` | 572 trades, 0 lost, +0.63c | 569 trades, 0 lost |
| `commodity_take98` | 202 trades, +1.21c | 203 trades, +1.26c |
| `commodity_early` | 741 trades, +1.11c (older +1.32c, newer +0.37c) | 753 trades, +1.02c (older +1.21c, newer +0.37c) |
| `crypto_early` | 696 trades, +0.55c (newer -0.45c) | 708 trades, +0.43c (newer -0.30c) |
| `commodity_mid` | 1,424 trades, +2.35c (newer -0.12c) | 1,426 trades, +1.91c with the self-bidding bug; +2.2c without, on a wider band |

## Where AI fits (three speeds)

1. **Daily research loop (in place).** The scorecard is the input; `docs/REVIEW_LOOP.md` is the protocol.
2. **Learned filter (data collection in place, model later).** Every fill stores its features: time into the
   window, spread, sizes, book imbalance, recent trade flow, index distance to the strike, the running
   settlement average. After two to three weeks there is enough to train a small model that predicts bad
   fills. The review's quick test of simple predictors found nothing stable on 2.5 days.
3. **Window gate (not built).** A once-per-window judgement on abnormal conditions. It should start in shadow
   mode: log its call, change nothing, be scored after two weeks.

A model call takes seconds and these decisions take milliseconds, and the edge is under 1c per contract, so
there is no model in the order path.

## Open questions

- Does a single-contract fill at a sub-cent price cost exactly its price, or is the balance rounded up to
  the cent? At 99c and above the answer decides whether small size can work at all.
- Do the WebSocket formats match the docs in every field? The REST audit on the page answers this within
  minutes of the first run with keys.
- Is the 99c-and-above result real? It needs about 1,500 trades with no more than a handful of losses.
