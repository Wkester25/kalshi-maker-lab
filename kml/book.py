"""One market's order book, kept from WebSocket snapshots and deltas (or set from a REST top-of-book poll).

Prices are integers in tenths of a cent ("mills": 987 = 98.7c) so they can be dictionary keys.
Kalshi lists bids only: YES bids, and NO bids. A NO bid at q is a YES ask at 100 - q.
Internally the NO side is stored at its YES-ask price.

Our own resting orders can be subtracted from a level before reading the best bid. The Late Maker bot
read the public book with its own bid in it, so it raised against itself and could not see the market
drop beneath it. That is what `own` fixes.
"""
from __future__ import annotations

FULL = 1000


def mills(x) -> int:
    """Dollars (string or float) -> tenths of a cent."""
    return int(round(float(x) * 1000))


def cents(m: int) -> float:
    return m / 10.0


class Book:
    def __init__(self, use_yes_price: bool = True):
        self.use_yes_price = use_yes_price
        self.yes: dict[int, float] = {}        # YES bid price -> size
        self.asks: dict[int, float] = {}       # YES ask price (= 100 - NO bid) -> size
        self.ready = False
        self.t = 0.0                           # local time of the last change
        self.exch_ms = 0                       # exchange time of the last change, if given
        self.crossed = 0                       # how many updates left bid >= ask (a sign of a format mismatch)
        self._bb = self._ba = None

    # ---- updates
    def _levels(self, msg: dict, side: str):
        for key in (f"{side}_dollars_fp", f"{side}_dollars"):
            if msg.get(key):
                return [(mills(p), float(q)) for p, q in msg[key]]
        if msg.get(side):                                              # legacy: [cents, count]
            return [(int(round(float(p) * 10)), float(q)) for p, q in msg[side]]
        return []

    def snapshot(self, msg: dict, t: float):
        self.yes = {p: q for p, q in self._levels(msg, "yes") if q > 0}
        no = self._levels(msg, "no")
        self.asks = {(p if self.use_yes_price else FULL - p): q for p, q in no if q > 0}
        self._changed(t, msg)

    def delta(self, msg: dict, t: float):
        if msg.get("price_dollars") not in (None, ""):
            p = mills(msg["price_dollars"])
        else:
            p = int(round(float(msg.get("price", 0)) * 10))
        d = float(msg["delta_fp"] if msg.get("delta_fp") not in (None, "") else msg.get("delta", 0))
        if msg.get("side") == "yes":
            side = self.yes
        else:
            side = self.asks
            if not self.use_yes_price:
                p = FULL - p
        v = side.get(p, 0.0) + d
        if v <= 1e-9:
            side.pop(p, None)
        else:
            side[p] = v
        self._changed(t, msg)

    def set_top(self, bid_c: float, ask_c: float, bid_size: float, ask_size: float, t: float):
        """Replace the book with one level each side (REST polling, replays)."""
        self.yes = {int(round(bid_c * 10)): bid_size} if bid_c > 0 else {}
        self.asks = {int(round(ask_c * 10)): ask_size} if 0 < ask_c < 100 else {}
        self._changed(t, {})

    def _changed(self, t: float, msg: dict):
        self.ready, self.t = True, t
        if msg.get("ts_ms"):
            self.exch_ms = int(msg["ts_ms"])
        self._bb = max(self.yes) if self.yes else None
        self._ba = min(self.asks) if self.asks else None
        if self._bb is not None and self._ba is not None and self._bb >= self._ba:
            self.crossed += 1

    # ---- reads
    def top(self):
        """(yes_bid, bid_size, yes_ask, ask_size) in mills; a missing side is None."""
        bb, ba = self._bb, self._ba
        return bb, (self.yes[bb] if bb is not None else 0.0), ba, (self.asks[ba] if ba is not None else 0.0)

    def favourite(self) -> str | None:
        bb, _, ba, _ = self.top()
        if bb is None or ba is None:
            return None
        return "YES" if (bb + ba) / 2 >= FULL / 2 else "NO"

    def quote(self, side: str, own: dict | None = None):
        """(bid, bid_size, ask, ask_size) for `side`, in that side's own prices (mills), with our own resting
        size removed. None if either side of the book is empty."""
        own = own or {}
        if side == "YES":
            bid = self._best(self.yes, True, {p: n for (s, p), n in own.items() if s == "YES"})
            ask = self._best(self.asks, False, {FULL - p: n for (s, p), n in own.items() if s == "NO"})
            if bid is None or ask is None:
                return None
            return bid[0], bid[1], ask[0], ask[1]
        q = self.quote("YES", own)
        if q is None:
            return None
        yb, ybs, ya, yas = q
        return FULL - ya, yas, FULL - yb, ybs

    @staticmethod
    def _best(levels: dict, highest: bool, own: dict):
        if not own:
            if not levels:
                return None
            p = max(levels) if highest else min(levels)
            return p, levels[p]
        for p in sorted(levels, reverse=highest):
            left = levels[p] - own.get(p, 0.0)
            if left > 1e-9:
                return p, left
        return None
