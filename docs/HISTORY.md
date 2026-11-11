# Project history and findings

Everything here was measured on Kalshi data recorded Nov 8-10, 2026 (about 2.5 days, Sunday midday to Tuesday
night). It is a short sample in one market regime. Treat every number as provisional until this program's own
recordings confirm or overturn it.

## The markets

Kalshi 15-minute up/down markets: 7 crypto (BTC, ETH, SOL, DOGE, HYPE, XRP, BNB) and 8 commodities (gold,
silver, platinum, palladium, copper, WTI, Brent, natural gas). YES pays 100c if the price ends above the strike.

- Crypto settles on the average of the CF Benchmarks index over the window's final 60 seconds. Kalshi streams
  that index, and the running final-minute average, on the `cfbenchmarks_value` WebSocket channel.
- Commodities settle on Pyth prices (confirmed for gold in Kalshi's rules; assumed for the rest). Kalshi
  streams them on `pyth_value`.
- Prices tick in 0.1c. Makers pay no fee on all 15 series (checked via the API on Nov 11: `fee_type` is
  `quadratic`, which charges takers only). Taker fee: ceil(7 x n x p x (1 - p)) cents for n contracts at
  price p in dollars, which rounds up to a full 1c on a single contract near 98c.

## What was tried before, and how it went

1. **Model-driven late maker (Nov 8-10, about 540 real trades, about -$26.50).** Copied from public hints by a
   Reddit trader (u/Weak_Wallaby734) claiming 99.81% wins. Rest a bid at 94-98.5c from 12:00 to 14:57 into the
   window when a price model gave one side 98%. It lost because Kalshi's price leads the model by 1-8 seconds
   (the model used an exchange average, not the settlement index), and late-window makers on crypto
   favourites lose 2-3c per contract to adverse selection.
2. **"Early favourite" mode (Nov 10).** Rest a bid on whichever side Kalshi prices at 97-99.5c (crypto, 9:00-
   12:00) or 96-99.5c (commodities, 9:00-13:00), 50c stop. A backtest claimed +1.0 to +1.4c per trade. First
   live window: 6 fills, 2 stops, -$1.06.

## What the independent review found (Nov 10-11)

1. **The backtest looked ahead.** The recording PC's clock was 1.08-1.2 s behind Kalshi (confirmed three ways:
   book-vs-trade alignment, the bot's own fills on Kalshi's trade tape, and `w32tm`). Book rows therefore
   showed the market about 1.2 s after their timestamp, and the simulated bot dodged fills it could not have
   dodged. The 59 trades the old simulator skipped averaged -6.7c. Corrected result: +0.79c on 13 markets
   (95% range +0.04 to +1.47), +0.61c on all 15 (-0.17 to +1.25).
2. **Fresh data, Nov 10 10:15-23:15 ET, never seen by any backtest:** the 13 traded markets made -0.03c on 322
   trades. Crypto -0.30c, commodities +0.21c. Pooled with the earlier data: +0.61c (range -0.09 to +1.22).
3. **Fills are selected against a slow maker.** Of the old bot's live bids: filled ones won 95.8%; ones that
   saw a print at their price but did not fill won 99-100%.
4. **The bot bid against itself.** It read the public book with its own order in it and raised one tick at a
   time; in 22% of 600 live chases the "best bid" it reacted to was its own.
5. **Who earns money (all 10.1M trades, no fill model):** crypto makers as a class -0.04c per contract over
   644M contracts; commodity makers +0.38c over 145M. Commodity makers buying the favourite at 55-90c in
   minutes 2-12 made +5.67c (+/- 0.93); makers on the underdog side lost about the same. Crypto: nothing
   significant anywhere for a passive maker.
6. **Tested and rejected:** buying favourites at the ask (52-96c, any minute); final-minute favourites at
   60-90c (a trade-timing artifact: buying blind at the ask lost 1.8-3.3c); momentum (26 of 27 commodity
   settings negative); BTC-moves-first cross-market signal; gating bids with the old price model; earlier
   commodity entry from 4:00 (+0.44c, worse than from 9:00); narrowing crypto to 9:00-11:00 (+1.04c in sample,
   +0.06c on fresh data).
7. **The Reddit trader's own numbers** ($50 to $3,000 over about 6,300 trades at 99.81% wins) imply an average
   entry near 99.4-99.7c, not 94-98c. At 99c and above on his 5 coins: 10:00-12:00 and the last 30 s showed 569
   simulated trades and no losses at settlement; 12:00-14:30 lost money even at 99c+. At 99.25c one loss erases
   about 132 wins, so zero losses in 569 is not proof.

## The five frozen rules (kml/config.py)

| Rule | Evidence when frozen (live-like simulation) |
| --- | --- |
| `crypto_99` | 572 trades, 0 lost, +0.63c (older and newer windows alike). The best crypto lead; thin by construction |
| `commodity_mid` | +2.35c on 1,424 trades but +3.12c older, -0.12c newer. Wide swings: 70c bids win 30c or lose 70c |
| `commodity_take98` | +1.21c on 202 trades, 1 loss. Taker; needs about 10 contracts so the fee rounding doesn't eat it |
| `commodity_early` | +1.11c on 741 trades (older +1.32c, newer +0.37c). The old bot's commodity leg |
| `crypto_early` | +0.55c on 696 (newer -0.45c). Kept as a control |

None of them is proven. The program exists to score them on fresh data with correct timing.

## Open questions, in order of importance

1. **Fee rounding on single sub-cent fills.** Does a 1-contract maker fill at 99.3c cost exactly 99.3c, or is
   the balance rounded up to 100c? Kalshi's docs say balance changes are rounded against the buyer to the
   account's precision ($0.0001 for direct members, $0.01 for others). If it rounds to the cent,
   `crypto_99` cannot make money at 1 contract. Check by comparing a real fill with the balance change.
2. **Do the WebSocket formats match the docs?** The program was written from Kalshi's docs, tested against a
   fake exchange, and never against the real one. It compares its WebSocket book with the REST book every
   minute; any "disagrees" error in the log means the book parsing is wrong (first suspect: `use_yes_price`).
3. **Is `crypto_99` real?** Needs about 1,500 trades with no more than a handful of losses.
4. **Is the commodity favourite effect capturable by a 1-second-latency maker?** The tape says +5.7c exists;
   how much of it a bot at this speed keeps is the question.
5. Weekdays, CPI and FOMC days, and EIA report days (Wed and Thu 10:30 ET, skipped for commodities) are not
   in the original sample.
