"""Replay recorded data through the real engine in paper mode, with a simulated clock.

Used two ways: to check the engine against the review's independent simulator, and to score rules on old
recordings (tools/replay_late_maker.py reads the Late Maker bot's CSV files).
"""
from __future__ import annotations

import heapq

from .engine import Engine, Market


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def replay_window(eng: Engine, clock: Clock, m: Market, rows, trades, result: str, book_shift: float = 0.0,
                  grid: float = 0.5):
    """rows: (secs, yes_bid_c, yes_ask_c, bid_size, ask_size); trades: (secs, yes_price_c, taker_side, count).
    book_shift: seconds to add to each row's time (the Late Maker recordings were stamped by a clock 1.2 s slow).
    result: 'yes' or 'no'."""
    ev = [(m.open_ts + s + book_shift, 0, i) for i, (s, *_r) in enumerate(rows)]
    ev += [(m.open_ts + s, 1, i) for i, (s, *_r) in enumerate(trades)]
    t0 = m.open_ts + (rows[0][0] if rows else 0)
    k = 0
    while t0 + k * grid < m.close_ts:
        ev.append((t0 + k * grid, 2, 0))
        k += 1
    heapq.heapify(ev)
    clock.t = t0
    eng.add_market(m)
    while ev:
        t, kind, i = heapq.heappop(ev)
        if t >= m.close_ts:
            break
        clock.t = t
        if kind == 0:
            _, b, a, bs, as_ = rows[i]
            eng.on_top(m.ticker, b, a, bs, as_)
        elif kind == 1:
            _, yp, side, cnt = trades[i]
            eng.on_trade(m.ticker, int(round(yp * 10)), cnt, side, t)
            continue
        eng.tick()
    clock.t = m.close_ts
    eng.expire(m.ticker)
    eng.settle(m.ticker, result)
