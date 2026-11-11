"""The trading engine: markets, rules, paper execution, settlement.

It does no network I/O itself. Market data is pushed in (on_snapshot / on_delta / on_top / on_trade), the clock
is injected, and live orders go through an executor object. That is what lets the same code be driven by the
live WebSocket, by REST polling, or by a replay of recorded data.

Every rule runs in paper mode unless the dashboard's live-trading switch is on AND the rule is marked "Trade live".
Paper execution uses the fill model from the review:
  * an order goes live `place_latency_ms` after the decision, and a cancel takes `cancel_latency_ms`;
  * a trade THROUGH our price always fills us;
  * a trade AT our price fills us only after the size that was displayed ahead of us has traded;
  * the market's bid is read with our own resting orders removed.
"""
from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import config as C
from .book import FULL, Book, cents

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                                    # noqa: BLE001 - Windows without tzdata
    ET = timezone(timedelta(hours=-5))


def taker_fee_c(n: float, px_mills: int) -> float:
    """Kalshi's taker fee in cents for an order of n contracts: round up(7 * n * p * (1 - p))."""
    p = px_mills / FULL
    return math.ceil(round(7 * n * p * (1 - p), 6))


def settle_pnl_c(side: str, px_mills: int, n: float, fee_c: float, result: str) -> tuple[bool, float]:
    """(won, cents for the whole position) for a position held to settlement. result: 'yes' or 'no'."""
    won = (result == "yes") == (side == "YES")
    return won, (n * (FULL - px_mills) / 10 if won else -n * px_mills / 10) - fee_c


class Market:
    def __init__(self, ticker: str, coin: str, group: str, open_ts: float, close_ts: float, strike: float | None,
                 use_yes_price: bool = True):
        self.ticker, self.coin, self.group = ticker, coin, group
        self.open_ts, self.close_ts, self.strike = open_ts, close_ts, strike
        self.book = Book(use_yes_price)
        self.flow: deque = deque(maxlen=400)           # (t, maker_side, count) of recent trades
        self.result: str | None = None
        self.news: str | None = None
        self.next_result_check = close_ts + 20
        self.trades = 0


@dataclass
class Order:
    side: str
    px: int
    n: float
    placed: float
    live_at: float
    queue: float = 0.0
    cancel_at: float | None = None
    why: str = ""
    at_px_vol: float = 0.0
    id: str = ""
    seen: float = 0.0


@dataclass
class Pos:
    side: str
    px: int
    n: float
    t: float
    fee_c: float = 0.0
    tid: int = 0


@dataclass
class State:
    rule: dict
    market: Market
    mode: str = "paper"
    order: Order | None = None
    pos: Pos | None = None
    done: bool = False
    busy: bool = False
    band_since: float | None = None
    low_since: float | None = None
    cool_until: float = 0.0
    funded: bool = False
    note: str = ""
    extra: dict = field(default_factory=dict)


class Engine:
    def __init__(self, cfg: dict, store, now=time.time, live_exec=None):
        self.cfg, self.store, self.now = cfg, store, now
        self.live_exec = live_exec
        self.markets: dict[str, Market] = {}
        self.by_coin: dict[str, Market] = {}
        self.states: dict[str, list[State]] = {}       # ticker -> states
        self.own: dict[str, dict] = {}                 # ticker -> {(side, px): size} of our real resting orders
        self.index: dict[str, dict] = {}               # CF Benchmarks index id -> latest values
        self.pyth: dict[str, dict] = {}
        self.live_halt: str | None = None
        self.started = time.time()

    # ------------------------------------------------------------------ helpers
    def event(self, level: str, msg: str):
        self.store.event(level, msg, self.now())

    def rule_mode(self, rule: dict) -> str:
        live = (self.live_exec is not None and self.cfg["live"].get("enabled")
                and rule.get("live") and rule["kind"] == "maker" and not self.live_halt)
        return "live" if live else "paper"

    def _news(self, m: Market) -> str | None:
        nw = self.cfg["news"]
        if not nw.get("skip"):
            return None
        o = datetime.fromtimestamp(m.open_ts, ET)
        c = datetime.fromtimestamp(m.close_ts, ET)
        if nw.get("weekdays_only") and o.weekday() >= 5:
            return None
        items = [{"time": t} for t in nw.get("times_et", [])] + list(nw.get("extra", []))
        for it in items:
            if it.get("group") and it["group"] != m.group:
                continue
            if it.get("weekday") is not None and int(it["weekday"]) != o.weekday():
                continue
            try:
                hh, mm = map(int, it["time"].split(":"))
            except (ValueError, KeyError):
                continue
            for base in (o, c):                         # a window can straddle midnight
                t = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if o <= t < c:
                    return f"{it.get('why', 'scheduled news')} at {it['time']} ET"
        return None

    # ------------------------------------------------------------------ markets
    def add_market(self, m: Market):
        if m.ticker in self.markets:
            return
        m.news = self._news(m)
        self.markets[m.ticker] = m
        self.by_coin[m.coin] = m
        self.store.market(m)
        sts = []
        for r in self.cfg["rules"]:
            if r.get("enabled") and m.coin in C.rule_coins(r, self.cfg):
                sts.append(State(rule=r, market=m, mode=self.rule_mode(r)))
        self.states[m.ticker] = sts
        self._resume(m, sts)

    def _resume(self, m: Market, sts: list[State]):
        """After a restart, pick up positions already taken in this market. An open one is held again (its stop
        and settlement carry on) and flagged; a rule that already traded here does not enter a second time."""
        prior = self.store.ticker_trades(m.ticker)
        for st in sts:
            for t in (x for x in prior if x["rule"] == st.rule["id"]):
                st.done = True
                if t["exit"] is not None or (t["mode"] == "live" and self.live_exec is None):
                    continue                    # closed, or live with no way to manage it: settled from the result
                st.mode, st.done = t["mode"], False
                st.pos = Pos(side=t["side"], px=int(round(t["px"] * 10)), n=t["n"], t=t["t_fill"],
                             fee_c=t["fee_c"] or 0.0, tid=t["id"])
                self.store.mark_restart(t["id"])
                self.event("info", f"resumed {t['mode']} {st.rule['id']} {t['side']} @ {t['px']:g} in {m.ticker} "
                                   "after a restart")

    def drop_market(self, ticker: str):
        m = self.markets.pop(ticker, None)
        self.states.pop(ticker, None)
        self.own.pop(ticker, None)
        if m and self.by_coin.get(m.coin) is m:
            self.by_coin.pop(m.coin, None)

    # ------------------------------------------------------------------ market data in
    def on_snapshot(self, ticker: str, msg: dict):
        m = self.markets.get(ticker)
        if m:
            m.book.snapshot(msg, self.now())

    def on_delta(self, ticker: str, msg: dict):
        m = self.markets.get(ticker)
        if m and m.book.ready:
            m.book.delta(msg, self.now())

    def on_top(self, ticker: str, bid_c: float, ask_c: float, bid_size: float, ask_size: float):
        m = self.markets.get(ticker)
        if m:
            m.book.set_top(bid_c, ask_c, bid_size, ask_size, self.now())

    def on_trade(self, ticker: str, yes_px: int, count: float, taker_side: str, t: float | None = None):
        """yes_px in mills. taker_side 'no' means the taker sold YES, so the resting maker bought YES."""
        m = self.markets.get(ticker)
        if not m:
            return
        t = t if t is not None else self.now()
        maker = "YES" if taker_side == "no" else "NO"
        mp = yes_px if maker == "YES" else FULL - yes_px
        m.flow.append((t, maker, count))
        m.trades += 1
        for st in self.states.get(ticker, ()):
            o = st.order
            if not o or st.mode != "paper" or st.pos or o.side != maker:
                continue
            if t < o.live_at or (o.cancel_at is not None and t >= o.cancel_at):
                continue
            if mp < o.px:
                self._filled(st, o.n, o.px, t)
            elif mp == o.px:
                o.at_px_vol += count
                o.queue -= count
                if o.queue < 0:
                    self._filled(st, o.n, o.px, t)

    def on_index(self, index_id: str, value: float, d: dict):
        self.index[index_id] = {"value": value, "t": self.now(), **d}

    def on_pyth(self, underlying: str, value: float, src_ms: int):
        self.pyth[underlying] = {"value": value, "t": self.now(), "src_ms": src_ms}

    # ------------------------------------------------------------------ the loop
    def tick(self):
        now = self.now()
        for ticker, sts in list(self.states.items()):
            m = self.markets[ticker]
            for st in sts:
                try:
                    self._eval(st, m, now)
                except Exception as e:                     # noqa: BLE001 - one bad state must not stop the rest
                    self.event("error", f"{st.rule['id']} {ticker}: {type(e).__name__}: {e}")

    def _features(self, m: Market, side: str, now: float) -> dict:
        q = m.book.quote(side)
        f = {"secs": round(now - m.open_ts, 1)}
        if q:
            bid, bsz, ask, asz = q
            f.update(spread=cents(ask - bid), bid_size=bsz, ask_size=asz, imb=round(bsz / (bsz + asz + 1e-9), 3))
        sold = sum(c for t, s, c in m.flow if now - t <= 5 and s == side)       # sold to our side's bids, last 5 s
        bought = sum(c for t, s, c in m.flow if now - t <= 5 and s != side)
        f.update(flow_sell_5s=sold, flow_buy_5s=bought)
        idx = self.index.get(self.cfg["markets"].get("crypto", {}).get(m.coin, {}).get("index", ""))
        if idx and m.strike:
            f.update(index=idx["value"], strike=m.strike, dist_bp=round((idx["value"] - m.strike) / m.strike * 1e4, 2),
                     index_age=round(now - idx["t"], 2))
            if idx.get("win_avg") is not None:
                f.update(settle_avg=idx["win_avg"], settle_n=idx.get("win_n"))
        return f

    def _eval(self, st: State, m: Market, now: float):
        r, o = st.rule, st.order
        secs = now - m.open_ts
        if st.busy:
            return
        if o and o.cancel_at is not None and st.mode == "paper":
            if now < o.cancel_at:
                return
            self._order_ended(st, "cancelled: " + o.why, now)
            o = None
        if st.pos:
            return self._check_stop(st, m, now, secs)
        if st.done or now < st.cool_until:
            return
        in_win = any(a <= secs <= b for a, b in r["windows"]) and now < m.close_ts - 2
        if not in_win or m.news or not r.get("enabled"):
            if o:
                self._cancel(st, "outside the bidding window" if not m.news else m.news, now)
            st.band_since = None
            st.note = m.news or ""
            return
        if not m.book.ready or (self.cfg.get("source") == "rest" and now - m.book.t > 30):
            if o:
                self._cancel(st, "no fresh book", now)
            return
        side = o.side if o else m.book.favourite()
        q = m.book.quote(side, self.own.get(m.ticker)) if side else None
        if q is None:
            if o:
                self._cancel(st, "one side of the book is empty", now)
            st.band_since = None
            return
        bid, bsz, ask, asz = q
        lo, hi = int(round(r["lo"] * 10)), int(round(r["hi"] * 10))
        if r["kind"] == "taker":
            if not (lo <= ask < hi):
                st.band_since = None
                return
            st.band_since = st.band_since or now
            if now - st.band_since < r["confirm_ms"] / 1000:
                return
            n = min(float(r["contracts"]), asz)
            if n >= 1:
                n = math.floor(n)
                self._filled(st, n, ask, now + self.cfg["paper"]["place_latency_ms"] / 1000, fee_c=taker_fee_c(n, ask),
                             kind="taker", side=side)
            return
        if not (lo <= bid < hi) or ask > FULL - 1:
            st.band_since = None
            if o:
                self._cancel(st, f"market left the band ({cents(bid):g}/{cents(ask):g})", now)
            return
        st.band_since = st.band_since or now
        if not o and now - st.band_since < r["confirm_ms"] / 1000:
            return
        px = bid
        if r.get("pricing", "improve") == "improve":
            px = min(bid + 1, ask - 1)
            if px <= bid or px >= hi:
                px = bid
        if o:
            if px > o.px:
                self._cancel(st, f"moving bid up {cents(o.px):g} -> {cents(px):g}", now)
            return
        self._place(st, m, side, px, float(r["contracts"]), bsz if px == bid else 0.0, now)

    # ------------------------------------------------------------------ orders
    def _place(self, st: State, m: Market, side: str, px: int, n: float, queue: float, now: float):
        if st.mode == "paper":
            st.order = Order(side, px, n, placed=now, live_at=now + self.cfg["paper"]["place_latency_ms"] / 1000,
                             queue=queue)
            return
        why = self._live_blocked(st, m, px, n, now)
        if why:
            st.note = why
            st.cool_until = now + 5
            return
        st.busy = True
        asyncio.ensure_future(self.live_exec.place(st, side, px, n, queue))

    def _cancel(self, st: State, why: str, now: float):
        o = st.order
        if not o or o.cancel_at is not None:
            return
        o.why = why
        if st.mode == "paper":
            o.cancel_at = now + self.cfg["paper"]["cancel_latency_ms"] / 1000
            return
        o.cancel_at = now
        st.busy = True
        asyncio.ensure_future(self.live_exec.cancel(st, why))

    def _order_ended(self, st: State, outcome: str, now: float):
        o, m = st.order, st.market
        if not o:
            return
        self.store.order(rule=st.rule["id"], mode=st.mode, ticker=m.ticker, coin=m.coin, close_ts=m.close_ts,
                         side=o.side, px=cents(o.px), n=o.n, t_place=o.placed, t_end=now,
                         secs=round(o.placed - m.open_ts, 1), outcome=outcome.split(":")[0], queue=o.queue,
                         at_px_vol=o.at_px_vol)
        own = self.own.get(m.ticker)
        if own:
            own.pop((o.side, o.px), None)
        st.order = None

    def _filled(self, st: State, n: float, px: int, t: float, fee_c: float = 0.0, kind: str | None = None,
                side: str | None = None):
        m, o = st.market, st.order
        if st.pos:                                          # a further partial fill of a live order
            st.pos.n += n
            return
        st.pos = Pos(side=side or (o.side if o else m.book.favourite()), px=px, n=n, t=t, fee_c=fee_c)
        st.pos.tid = self.store.open_trade(
            rule=st.rule["id"], mode=st.mode, ticker=m.ticker, coin=m.coin, grp=m.group, close_ts=m.close_ts,
            side=st.pos.side, px=cents(px), n=n, kind=kind or st.rule["kind"], t_place=(o.placed if o else t), t_fill=t,
            secs=round(t - m.open_ts, 1), fee_c=fee_c, feat=self._features(m, st.pos.side, t))
        if o:
            own = self.own.get(m.ticker)
            if own:
                own.pop((o.side, o.px), None)
            if st.mode == "paper" or o.n <= n:
                st.order = None
        st.low_since = None

    def _check_stop(self, st: State, m: Market, now: float, secs: float):
        r, p = st.rule, st.pos
        if st.order and st.mode == "live" and not st.busy:        # partly filled: pull the rest
            self._cancel(st, "position opened", now)
            return
        stop = float(r.get("stop_c") or 0)
        if not stop or secs >= 899 or st.done:
            return
        q = m.book.quote(p.side, self.own.get(m.ticker))
        if q is None:
            return                                          # nobody to sell to; keep the counter as it is
        bid = q[0]
        if bid >= stop * 10:
            st.low_since = None
            return
        st.low_since = st.low_since or now
        if now - st.low_since < r.get("stop_ms", 2000) / 1000:
            return
        if st.mode == "paper":
            got = max(bid - int(round(self.cfg["paper"]["stop_slip_c"] * 10)), 10)
            fee = taker_fee_c(p.n, got)
            pnl = p.n * (got - p.px) / 10 - fee - p.fee_c
            self.store.close_trade(p.tid, "stopped", pnl, exit_px=cents(got), t_exit=now, fee_c=fee + p.fee_c)
            st.pos, st.done = None, True
        else:
            st.busy = True
            asyncio.ensure_future(self.live_exec.stop_sell(st, bid))

    # ------------------------------------------------------------------ live guards
    def day_start(self) -> float:
        d = datetime.fromtimestamp(self.now(), ET).replace(hour=0, minute=0, second=0, microsecond=0)
        return d.timestamp()

    def _live_blocked(self, st: State, m: Market, px: int, n: float, now: float) -> str | None:
        lv = self.cfg["live"]
        if self.live_halt:
            return f"live trading halted: {self.live_halt}"
        if n * px / 1000 > lv["max_order_dollars"]:
            return "order is larger than max_order_dollars"
        if -self.store.day_pnl_c("live", self.day_start()) / 100 >= lv["max_day_loss_dollars"]:
            return "daily loss limit reached"
        s = self.store.scoreboard(st.rule["id"], "live")
        if s.get("n") and -s["total_c"] / 100 / max(float(st.rule["contracts"]), 1) >= lv["kill_switch_dollars_per_contract"]:
            return "kill switch: rule is down more than the limit per contract"
        open_cost = sum(s2.pos.n * s2.pos.px / 1000 for sts in self.states.values() for s2 in sts
                        if s2.mode == "live" and s2.pos)
        open_cost += sum(s2.order.n * s2.order.px / 1000 for sts in self.states.values() for s2 in sts
                         if s2.mode == "live" and s2.order)
        if open_cost + n * px / 1000 > lv["bankroll_dollars"] * lv["tail_fraction"]:
            return "tail cap: open risk would exceed tail_fraction of the bankroll"
        return None

    def stop_live_orders(self, why: str):
        """Cancel every resting live order (positions are held to settlement)."""
        now = self.now()
        for sts in self.states.values():
            for st in sts:
                if st.mode == "live" and st.order and not st.busy:
                    self._cancel(st, why, now)
                if st.mode == "live" and not st.pos:
                    st.done = True                       # no new live bids in this window

    def halt_live(self, why: str):
        if not self.live_halt:
            self.live_halt = why
            self.event("halt", f"LIVE TRADING HALTED: {why}")

    # ------------------------------------------------------------------ window end and settlement
    def expire(self, ticker: str):
        """The window closed: resting paper orders die, positions wait for the result."""
        now = self.now()
        for st in self.states.get(ticker, ()):
            if st.order and st.mode == "paper":
                self._order_ended(st, "expired", now)
            st.done = st.done or st.pos is None

    def settle(self, ticker: str, result: str):
        """result: 'yes' or 'no'."""
        m = self.markets.get(ticker)
        if not m:
            return
        m.result = result
        now = self.now()
        self.expire(ticker)
        self.store.result(ticker, result)
        for st in self.states.get(ticker, ()):
            p = st.pos
            if not p:
                continue
            won, pnl = settle_pnl_c(p.side, p.px, p.n, p.fee_c, result)
            self.store.close_trade(p.tid, "won" if won else "lost", pnl, result=result, t_exit=now)
            st.pos = None
        self.drop_market(ticker)

    # ------------------------------------------------------------------ for the web page
    def status(self) -> dict:
        now = self.now()
        mk = []
        for m in sorted(self.markets.values(), key=lambda x: (x.group, x.coin, x.close_ts)):
            bb, bs, ba, as_ = m.book.top()
            idx = self.index.get(self.cfg["markets"].get("crypto", {}).get(m.coin, {}).get("index", ""))
            rules = []
            for st in self.states.get(m.ticker, ()):
                s = "idle"
                if st.pos:
                    s = f"holding {st.pos.side} {st.pos.n:g} @ {cents(st.pos.px):g}"
                elif st.order:
                    s = f"bid {st.order.side} {cents(st.order.px):g}" + (" (cancelling)" if st.order.cancel_at else "")
                elif st.done:
                    s = "done"
                elif st.note:
                    s = st.note
                rules.append({"rule": st.rule["id"], "mode": st.mode, "state": s})
            mk.append({"coin": m.coin, "group": m.group, "ticker": m.ticker, "secs": round(now - m.open_ts),
                       "left": round(m.close_ts - now), "strike": m.strike,
                       "bid": cents(bb) if bb is not None else None, "ask": cents(ba) if ba is not None else None,
                       "bid_size": bs, "ask_size": as_, "book_age": round(now - m.book.t, 1) if m.book.ready else None,
                       "crossed": m.book.crossed, "trades": m.trades, "news": m.news,
                       "index": idx["value"] if idx else None,
                       "settle_avg": idx.get("win_avg") if idx else None, "rules": rules})
        return {"now": now, "uptime": round(time.time() - self.started), "markets": mk, "live_halt": self.live_halt}
