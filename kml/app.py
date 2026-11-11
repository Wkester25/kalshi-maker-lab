"""Wires everything together: REST client, WebSocket (or REST polling), engine, recorder, web page."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from . import config as C
from .book import mills
from .engine import ET, Engine, Market
from .kalshi_rest import Client, KalshiError, iso_ts, num
from .live import LiveExec
from .recorder import Recorder
from .store import Store

ROOT = Path(__file__).resolve().parent.parent


class App:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.client = Client(cfg.get("env", "real"))
        self.store = Store(ROOT / "data" / "lab.db")
        self.rec = Recorder(cfg, ROOT)
        self.offset = 0.0                       # exchange clock minus this machine's clock, seconds
        self._off_samples: list[tuple[float, float]] = []
        self.live = LiveExec(self.client) if self.client.has_keys else None
        self.engine = Engine(cfg, self.store, now=self.exch_now, live_exec=self.live)
        if self.live:
            self.live.engine = self.engine
        self.ws = None
        self.source = cfg.get("source", "ws")
        self.coins = C.coins(cfg)
        self.next_try: dict[str, float] = {}
        self.audit = {"checked": 0, "mismatch": 0, "last": ""}
        self.rest_seen: dict[str, float] = {}
        self._pyth_last: dict[str, float] = {}
        self.stopping = False
        self.notes: list[str] = []

    def exch_now(self) -> float:
        return time.time() + self.offset

    def event(self, level: str, msg: str):
        self.store.event(level, msg)
        if level in ("error", "halt", "warn"):
            print(f"[{time.strftime('%H:%M:%S')}] {level.upper()}: {msg}")

    # ------------------------------------------------------------------ WebSocket data
    def on_ws(self, typ: str, msg: dict, recv: float, sending_ms) -> bool:
        if sending_ms:
            self._clock_sample(recv, sending_ms / 1000 - recv)
        e = self.engine
        if typ == "orderbook_snapshot" or typ == "orderbook_delta":
            tk = msg.get("market_ticker")
            m = e.markets.get(tk)
            if not m:
                return True
            (e.on_snapshot if typ == "orderbook_snapshot" else e.on_delta)(tk, msg)
            self.rec.top(recv, tk, m.book)
            return True
        if typ == "trade":
            tk = msg.get("market_ticker")
            if msg.get("yes_price_dollars") not in (None, ""):
                yp = mills(msg["yes_price_dollars"])
            else:
                yp = int(round(float(msg.get("yes_price", 0)) * 10))
            cnt = float(msg["count_fp"] if msg.get("count_fp") not in (None, "") else msg.get("count", 0))
            side = msg.get("taker_side") or msg.get("taker_outcome_side") or ""
            ts_ms = msg.get("ts_ms") or (msg.get("ts", 0) * 1000)
            e.on_trade(tk, yp, cnt, side, ts_ms / 1000 if ts_ms else None)
            self.rec.trade(recv, ts_ms, tk, yp / 10, cnt, side)
            return True
        if typ in ("cfbenchmarks_value", "cfbenchmarks_value_5hz"):
            iid = msg.get("index_id", "")
            val, src = None, msg.get("source_ts_ms")
            if msg.get("value_usd") not in (None, ""):
                val = float(msg["value_usd"])
            elif msg.get("data"):
                try:
                    raw = json.loads(msg["data"])
                    val, src = float(raw.get("value")), raw.get("time", src)
                except (ValueError, TypeError):
                    pass
            if val is None:
                return True
            a, w = msg.get("avg_60s_data") or {}, msg.get("last_60s_windowed_average_15min") or {}
            d = {"src_ms": src, "avg60": _f(a.get("value")), "avg60_n": a.get("window_size"),
                 "win_avg": _f(w.get("value")), "win_n": w.get("window_size")}
            if typ == "cfbenchmarks_value" or iid not in e.index:
                e.on_index(iid, val, d)
            else:                                   # the 5 Hz feed carries no averages: keep the last ones
                e.index[iid].update(value=val, t=e.now(), src_ms=src)
            self.rec.index(recv, iid, val, src, d["avg60"], d["avg60_n"], d["win_avg"], d["win_n"])
            return True
        if typ == "pyth_value":
            u = msg.get("underlying_ticker", "")
            try:
                val = float(msg.get("value_usd"))
            except (TypeError, ValueError):
                return True
            e.on_pyth(u, val, msg.get("source_ts_ms") or 0)
            if recv - self._pyth_last.get(u, 0) >= 1.0:              # one row a second per underlying is plenty
                self._pyth_last[u] = recv
                self.rec.pyth(recv, u, val, msg.get("source_ts_ms") or 0)
            return True
        if typ and (typ.endswith("_indexlist") or typ.endswith("_underlying_list")):
            self.event("info", f"{typ}: {json.dumps(msg)[:400]}")
            return True
        return False

    def _clock_sample(self, recv: float, d: float):
        """d = exchange send time - our receive time = clock offset - network delay. The largest recent d is the
        sample with the least delay, so it is the best estimate of the offset."""
        s = self._off_samples
        s.append((recv, d))
        if len(s) > 400:
            cut = recv - 120
            self._off_samples = s = [x for x in s if x[0] >= cut][-300:]
        self.offset = max(x[1] for x in s)

    # ------------------------------------------------------------------ markets coming and going
    async def rotation(self):
        while not self.stopping:
            now = self.exch_now()
            e = self.engine
            for coin, info in self.coins.items():
                m = e.by_coin.get(coin)
                if m and now < m.close_ts:
                    if m.strike is None and now >= self.next_try.get(coin, 0):
                        self.next_try[coin] = now + 10
                        asyncio.ensure_future(self._strike(m))
                    continue
                if m:
                    e.expire(m.ticker)
                    e.by_coin.pop(coin, None)
                if now < self.next_try.get(coin, 0):
                    continue
                self.next_try[coin] = now + 2
                asyncio.ensure_future(self._open(coin, info))
            if self.ws:
                await self.ws.set_markets([m.ticker for m in e.markets.values() if now < m.close_ts])
            await asyncio.sleep(0.5)

    async def _open(self, coin: str, info: dict):
        try:
            d = await asyncio.to_thread(self.client.open_market, info["series"])
        except Exception as err:                                      # noqa: BLE001
            self.next_try[coin] = self.exch_now() + 5
            if "429" not in str(err):
                self.notes.append(f"{coin}: {str(err)[:80]}")
            return
        if not d or d["ticker"] in self.engine.markets:
            return
        strike = d.get("floor_strike")
        m = Market(d["ticker"], coin, info["group"], iso_ts(d["open_time"]), iso_ts(d["close_time"]),
                   float(strike) if strike not in (None, "") else None, self.cfg.get("use_yes_price", True))
        self.engine.add_market(m)
        self.rec.market(time.time(), m)

    async def _strike(self, m: Market):
        try:
            d = await asyncio.to_thread(self.client.market, m.ticker)
            if d and d.get("floor_strike") not in (None, ""):
                m.strike = float(d["floor_strike"])
        except Exception:                                             # noqa: BLE001
            pass

    async def results(self):
        while not self.stopping:
            now = self.exch_now()
            for m in list(self.engine.markets.values()):
                if now < m.next_result_check:
                    continue
                m.next_result_check = now + 15
                try:
                    d = await asyncio.to_thread(self.client.market, m.ticker) or {}
                except Exception:                                     # noqa: BLE001
                    continue
                res = str(d.get("result", "")).lower()
                if res in ("yes", "no"):
                    self.rec.market(time.time(), m, res)
                    self.engine.settle(m.ticker, res)
                elif now > m.close_ts + 3600:
                    self.event("warn", f"{m.ticker}: no result an hour after the close; open trades left unresolved")
                    for st in self.engine.states.get(m.ticker, ()):
                        if st.pos:
                            self.store.close_trade(st.pos.tid, "unresolved", 0.0, t_exit=now)
                    self.engine.drop_market(m.ticker)
            await asyncio.sleep(2)

    # ------------------------------------------------------------------ REST polling (no keys, or as a fallback)
    async def rest_feed(self):
        self.event("info", "market data: REST polling, once a second (no WebSocket)")
        sem = asyncio.Semaphore(12)

        async def one(m: Market, trades: bool):
            async with sem:
                try:
                    b = await asyncio.to_thread(self.client.book, m.ticker)
                    if b:
                        self.engine.on_top(m.ticker, b["bid"], b["ask"], b["bid_size"], b["ask_size"])
                        self.rec.top(time.time(), m.ticker, m.book)
                    if trades:
                        since = self.rest_seen.get(m.ticker, m.open_ts)
                        d = await asyncio.to_thread(self.client.get_public, "/markets/trades",
                                                    {"ticker": m.ticker, "limit": 200, "min_ts": int(since)})
                        rows = []
                        for t in d.get("trades") or []:
                            ts = iso_ts(t["created_time"])
                            if ts <= since:
                                continue
                            yp = mills(t["yes_price_dollars"]) if t.get("yes_price_dollars") not in (None, "") \
                                else int(round(float(t.get("yes_price", 0)) * 10))
                            cnt = float(t.get("count_fp") or t.get("count") or 0)
                            rows.append((ts, yp, cnt, t.get("taker_side", "")))
                        for ts, yp, cnt, side in sorted(rows):
                            self.engine.on_trade(m.ticker, yp, cnt, side, ts)
                            self.rec.trade(time.time(), int(ts * 1000), m.ticker, yp / 10, cnt, side)
                            self.rest_seen[m.ticker] = max(self.rest_seen.get(m.ticker, 0), ts)
                except (KalshiError, OSError, ValueError, KeyError):
                    pass

        k = 0
        while not self.stopping:
            t0 = time.time()
            now = self.exch_now()
            ms = [m for m in self.engine.markets.values() if now < m.close_ts]
            await asyncio.gather(*(one(m, (k + i) % 3 == 0) for i, m in enumerate(ms)))
            k += 1
            await asyncio.sleep(max(0.05, 1.0 - (time.time() - t0)))

    async def audit_loop(self):
        """Compare the WebSocket book with the REST book once a minute. They are different code paths, so
        agreement is real evidence the WebSocket book is being read correctly."""
        i = 0
        while not self.stopping:
            await asyncio.sleep(60)
            ms = [m for m in self.engine.markets.values() if m.book.ready and self.exch_now() < m.close_ts - 30]
            if not ms or not self.ws or not self.ws.connected:
                continue
            m = ms[i % len(ms)]
            i += 1
            try:
                b = await asyncio.to_thread(self.client.book, m.ticker)
            except Exception:                                         # noqa: BLE001
                continue
            bb, _, ba, _ = m.book.top()
            if not b or bb is None or ba is None:
                continue
            self.audit["checked"] += 1
            if abs(bb / 10 - b["bid"]) > 1.0 or abs(ba / 10 - b["ask"]) > 1.0:
                self.audit["mismatch"] += 1
                self.audit["last"] = (f"{m.ticker}: WebSocket {bb / 10:g}/{ba / 10:g} vs REST "
                                      f"{b['bid']:g}/{b['ask']:g}")
                if self.audit["mismatch"] in (3, 10, 30):
                    self.event("error", "WebSocket book disagrees with the REST book: " + self.audit["last"] +
                               ". If this persists, set use_yes_price to false (or source to rest) and restart.")

    # ------------------------------------------------------------------ loops
    async def eval_loop(self):
        step = max(self.cfg.get("eval_ms", 200), 50) / 1000
        while not self.stopping:
            self.engine.tick()
            await asyncio.sleep(step)

    async def housekeeping(self):
        from . import scorecard
        last_day = None
        while not self.stopping:
            await asyncio.sleep(5)
            self.rec.flush()
            self.notes = self.notes[-20:]
            from datetime import datetime
            d = datetime.fromtimestamp(time.time(), ET)
            if d.hour == 0 and d.minute >= 5 and last_day != d.date():
                last_day = d.date()
                try:
                    await asyncio.to_thread(scorecard.write, self.store, self.cfg, ROOT / "reports")
                except Exception as err:                              # noqa: BLE001
                    self.event("error", f"scorecard: {err}")

    def reload_keys(self) -> bool:
        """Pick up changed API keys without a restart where possible. Returns True if a restart is still needed."""
        self.client.__init__(self.cfg.get("env", "real"))
        if self.client.has_keys:
            if self.live is None:
                self.live = LiveExec(self.client)
                self.live.engine = self.engine
                self.engine.live_exec = self.live
            if self.ws:
                self.ws.signer, self.ws.key_id = self.client.signer, self.client.key_id
                asyncio.ensure_future(self.ws.reconnect("API key changed"))
                return False
            return self.cfg.get("source", "ws") == "ws"          # running on REST because there were no keys
        if self.cfg["live"].get("enabled"):
            self.engine.halt_live("API key removed")
        return self.ws is not None

    async def test_keys(self, env: str) -> dict:
        c = Client(env)
        if not c.has_keys:
            return {"ok": False, "error": c.key_error or "no key"}
        t0 = time.perf_counter()
        try:
            b = await asyncio.to_thread(c.balance)
        except KalshiError as e:
            why = str(e)
            if e.status in (401, 403):
                why = ("Kalshi refused the key. Check the key ID matches this private key, and that the key "
                       "hasn't been deleted on Kalshi")
            return {"ok": False, "error": why}
        except Exception as e:                                        # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        ms = round((time.perf_counter() - t0) * 1000)
        cents = num(b, "balance", None)
        return {"ok": True, "balance": None if cents is None else round(cents / 100, 2), "ms": ms}

    def feed_status(self) -> dict:
        s = {"source": self.source, "clock_offset_ms": round(self.offset * 1000), "audit": self.audit,
             "recorder_rows": self.rec.rows, "record_mode": self.rec.mode if self.rec.on else "off",
             "keys": self.client.has_keys, "key_error": self.client.key_error, "notes": self.notes[-5:]}
        if self.ws:
            s["ws"] = self.ws.status()
        return s

    async def run(self, web_runner=None):
        from .ws import KalshiWS
        if self.source == "ws" and not self.client.has_keys:
            self.event("warn", f"no API keys ({self.client.key_error}): using REST polling instead of the WebSocket")
            self.source = "rest"
        tasks = [self.rotation(), self.results(), self.eval_loop(), self.housekeeping()]
        if self.source == "ws":
            self.ws = KalshiWS(self.cfg["ws_url"], self.client.signer, self.client.key_id, self.cfg["ws_sign_path"],
                               self.on_ws, self.event, self.cfg.get("use_yes_price", True), on_raw=self.rec.raw)
            r = self.cfg["record"]
            idx = sorted({v["index"] for v in self.cfg["markets"].get("crypto", {}).values() if v.get("index")})
            if r.get("index", True) and idx:
                self.ws.want("cfbenchmarks_value", {"index_ids": idx})
                if r.get("index_5hz"):
                    self.ws.want("cfbenchmarks_value_5hz", {"index_ids": idx})
            if r.get("pyth"):
                self.ws.want("pyth_value", {"underlying_tickers": list(r["pyth"])})
            tasks += [self.ws.run(), self.audit_loop()]
        else:
            tasks.append(self.rest_feed())
        self.event("info", f"started: source {self.source}, live trading "
                           f"{'ON (dashboard switch)' if self.cfg['live'].get('enabled') else 'off'}")
        try:
            await asyncio.gather(*tasks)
        finally:
            self.stopping = True
            self.rec.close()


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
