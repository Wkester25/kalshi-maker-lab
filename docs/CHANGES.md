# Changes

One entry per change: date, what, why, and what result on later data counts as success or failure.
Judge each change only on data recorded after it.

## 2026-11-11: program fixes (no rule changed)

Found in the first day's health check. None of these touches a rule's band, window or markets.

1. **Positions open across a restart never settled.** Their state was only in memory, so 16 trades (14 of them
   `commodity_mid`) stayed open forever and were left out of every result: `commodity_mid` showed +4.37c on 19
   trades; with them it was -2.92c on 34. Now a restarted program resumes open positions in the current window
   (stop and settlement carry on, and a rule that already traded there does not enter again), and settles ones
   from past windows from the market result. All of them are flagged `restart = 1`.
   *Success:* no trade stays open more than ~2 minutes past its window's result.
2. **Reconnect storm at every roll-over.** The sequence check counted only snapshots and deltas, but Kalshi's
   reply to `update_subscription` appears to take a sequence number on the order-book stream, so each market
   added at a roll-over looked like a lost message (about 13 reconnects in the first 45 s of every window, each
   gap exactly one number, each at the second a market was added). Now every message on that subscription counts,
   and the first such reply is logged ("order-book ok reply carries seq ...") to confirm the explanation.
   *Success:* zero "missed order-book messages" warnings at roll-overs. *Failure:* the warnings continue with
   "at ok" in them, which means the reply's seq is not part of the stream.
3. **One-sided books were never recorded.** `recorder.top` raised on an empty side (0 of 1.59M rows had one),
   and handler errors were kept only in memory. Now an empty side is a blank, and handler errors are logged as
   events (1st, 10th, 100th ... occurrence). *Success:* `top_*.csv` has rows with a blank side, near window ends.
4. **Scoring start.** `score_since` in `kml/config.py` sets where reports start counting; set it to the first
   window that opened after these fixes were running. Earlier data (REST polling 11:02-11:17 ET, restarts mid-window)
   stays in the database and shows with `tools/analyze.py --all`.
