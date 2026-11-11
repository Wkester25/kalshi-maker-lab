"""Real orders through Kalshi's REST API. OFF unless three switches are all on (see README):
  1. the program was started with --allow-live
  2. "live.enabled" is true in the settings
  3. the individual rule has "live": true

The order sequences are the ones the Late Maker bot used with real money: post-only resting buy, cancel and
confirm (counting fills that landed first), reduce-only IOC sell for the stop. If a cancel cannot be confirmed,
every order is cancelled and live trading halts until a person restarts it.

This module has not itself traded on a live account. First use: one rule, one contract.
"""
from __future__ import annotations

import asyncio

from .book import cents
from .engine import Order
from .kalshi_rest import KalshiError, num

DONE = ("canceled", "cancelled", "executed", "expired")


class LiveExec:
    def __init__(self, client):
        self.c = client
        self.engine = None
        self.polls: dict[int, asyncio.Task] = {}

    async def _t(self, fn, *a):
        return await asyncio.to_thread(fn, *a)

    # ---- place
    async def place(self, st, side: str, px: int, n: float, queue: float):
        e, m = self.engine, st.market
        try:
            pos = await self._t(self.c.position, m.ticker)
            if (side == "YES" and pos < 0) or (side == "NO" and pos > 0):
                st.done, st.note = True, "account holds the other side of this market"
                return
            if not st.funded:
                ok, msg = await self._t(self.c.ensure_shard_cash, m.ticker, n * cents(px), n * cents(px) * 3)
                if not ok:
                    st.note, st.cool_until = f"can't fund: {msg}", e.now() + 20
                    return
                st.funded = True
            if e.now() > m.close_ts - 3:
                return
            o = await self._t(self.c.place_resting_buy, m.ticker, side, int(n), cents(px), m.close_ts - 2)
        except KalshiError as err:
            e.event("error", f"live place {m.ticker}: {err}")
            st.cool_until = e.now() + 2
            return
        except Exception as err:                                      # noqa: BLE001
            e.event("error", f"live place {m.ticker}: {type(err).__name__}: {err}")
            st.cool_until = e.now() + 5
            return
        finally:
            st.busy = False
        if o.get("rejected"):
            st.note = o["rejected"]
            return
        now = e.now()
        order = Order(side, px, n, placed=now, live_at=now, queue=queue, id=o.get("order_id", ""))
        got = float(o.get("fill_count") or 0)
        e.event("trade", f"LIVE bid {m.ticker} {side} {n:g} @ {cents(px):g}c ({st.rule['id']})")
        if got < n:
            st.order = order
            e.own.setdefault(m.ticker, {})[(side, px)] = n - got
            order.seen = got
            self.polls[id(st)] = asyncio.ensure_future(self._poll(st, order))
        else:
            st.order = order
        if got:
            e._filled(st, got, px, now)

    async def _poll(self, st, order: Order):
        e = self.engine
        try:
            while st.order is order and order.cancel_at is None:
                await asyncio.sleep(1.0)
                if st.order is not order or order.cancel_at is not None:
                    return
                r = await self._t(self.c.get_order, order.id)
                f = float(num(r, "fill_count", 0) or 0)
                if f > order.seen:
                    e._filled(st, f - order.seen, order.px, e.now())
                    order.seen = f
                    e.event("trade", f"LIVE fill {st.market.ticker} {order.side} {f:g} @ {cents(order.px):g}c")
                if f >= order.n or str(r.get("status", "")).lower() in DONE:
                    if st.order is order:
                        e._order_ended(st, "filled" if f >= order.n else "expired", e.now())
                    return
        except KalshiError as err:
            e.event("error", f"live poll {st.market.ticker}: {err}")
        except asyncio.CancelledError:
            pass

    # ---- cancel
    async def cancel(self, st, why: str):
        e, o, m = self.engine, st.order, st.market
        try:
            if not o:
                return
            ok = False
            try:
                await self._t(self.c.cancel, o.id, m.ticker)
                ok = True
            except KalshiError as err:
                if err.status not in (404, 410):
                    raise
            status = ""
            for _ in range(8):
                r = await self._t(self.c.get_order, o.id)
                f = float(num(r, "fill_count", 0) or 0)
                if f > o.seen:
                    e._filled(st, f - o.seen, o.px, e.now())
                    o.seen = f
                status = str(r.get("status", "")).lower()
                if status in DONE or (status == "not_visible_yet" and ok):
                    break
                await asyncio.sleep(0.4)
            else:
                try:
                    await self._t(self.c.cancel_all)
                    note = "cancel-all sent"
                except KalshiError as err:
                    note = f"cancel-all failed ({err}): cancel open orders on kalshi.com"
                e.halt_live(f"order {o.id[:8]} on {m.ticker} still {status}; {note}")
            e._order_ended(st, "cancelled: " + why, e.now())
        except Exception as err:                                      # noqa: BLE001
            e.event("error", f"live cancel {m.ticker}: {type(err).__name__}: {err}")
            e.halt_live(f"cancel failed on {m.ticker}: {err}")
        finally:
            st.busy = False

    # ---- stop
    async def stop_sell(self, st, bid: int):
        e, p, m = self.engine, st.pos, st.market
        try:
            floor = max(cents(bid) - 5.0, 0.1)
            o = await self._t(self.c.sell_ioc, m.ticker, p.side, int(p.n), round(floor, 1))
            n = float(o.get("fill_count") or 0)
            if n:
                px = o.get("avg_price_c") or cents(bid)
                fee = float(o.get("fees_c") or 0.0)
                x = st.extra
                x.setdefault("n0", p.n)
                x["proceeds"] = x.get("proceeds", 0.0) + n * px - fee
                x["fees"] = x.get("fees", 0.0) + fee
                p.n -= n
                e.event("trade", f"LIVE stop {m.ticker} sold {n:g} @ {px:g}c")
                if p.n <= 0:
                    pnl = x["proceeds"] - x["n0"] * cents(p.px)
                    e.store.close_trade(p.tid, "stopped", pnl, exit_px=px, t_exit=e.now(), fee_c=x["fees"])
                    st.pos, st.done = None, True
        except Exception as err:                                      # noqa: BLE001
            e.event("error", f"live stop {m.ticker}: {type(err).__name__}: {err}")
        finally:
            st.busy = False

    async def cancel_everything(self) -> str:
        try:
            await self._t(self.c.cancel_all)
            return "cancel-all sent"
        except Exception as err:                                      # noqa: BLE001
            return f"cancel-all failed: {err}"
