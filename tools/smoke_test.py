#!/usr/bin/env python3
"""Offline self-test: runs the whole program against a fake Kalshi on this machine for about 40 seconds.

No keys, no network. It checks the pieces that unit tests can't: the WebSocket client (connect, subscribe,
add and remove markets as windows roll over, snapshots, deltas, trades, index values), the recorder,
settlement, and the web API.

  python tools/smoke_test.py            run and print PASS or FAIL
  python tools/smoke_test.py --serve    keep it running so you can look at the web page
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
TMP = tempfile.mkdtemp(prefix="kml_smoke_")
os.environ["KML_CONFIG"] = str(Path(TMP) / "config.json")
os.environ["KML_KEYS_FILE"] = str(Path(TMP) / "keys.json")

from aiohttp import web                          # noqa: E402

import kml.app as A                              # noqa: E402
from kml import config as C                      # noqa: E402
from kml.web import make_app                     # noqa: E402

A.ROOT = Path(TMP)
WINDOW = 900
T0 = time.time()


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeKalshi:
    """Windows are 15 minutes long, but the first one is already 14:25 old, so it closes 35 s after the start."""

    def __init__(self):
        self.clients: list[web.WebSocketResponse] = []
        self.subs: dict[web.WebSocketResponse, dict] = {}
        self.seq: dict[tuple, int] = {}

    def window(self, now: float):
        k = int((now - (T0 - 865)) // WINDOW)
        o = T0 - 865 + k * WINDOW
        return k, o, o + WINDOW

    def market(self, series: str, now: float) -> dict:
        k, o, c = self.window(now)
        return {"ticker": f"{series}-W{k}", "open_time": iso(o), "close_time": iso(c), "floor_strike": 70000.0}

    def open_market(self, series: str):
        return self.market(series, time.time())

    def market_by_ticker(self, ticker: str):
        series, k = ticker.rsplit("-W", 1)
        o = T0 - 865 + int(k) * WINDOW
        d = {"ticker": ticker, "open_time": iso(o), "close_time": iso(o + WINDOW), "floor_strike": 70000.0}
        if time.time() > o + WINDOW + 1:
            d["result"] = "yes"
        return d

    def book(self, ticker: str):
        return {"bid": 99.2, "ask": 99.3, "bid_size": 500.0, "ask_size": 400.0}

    async def ws(self, req: web.Request):
        assert req.headers.get("KALSHI-ACCESS-KEY") == "smoke", "auth headers missing"
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        self.clients.append(ws)
        self.subs[ws] = {}
        sid = 0
        async for msg in ws:
            d = json.loads(msg.data)
            p = d.get("params", {})
            if d["cmd"] == "subscribe":
                for ch in p["channels"]:
                    sid += 1
                    self.subs[ws][ch] = {"sid": sid, "tickers": set(p.get("market_tickers", [])), "p": p}
                    await ws.send_json({"id": d["id"], "type": "subscribed", "msg": {"channel": ch, "sid": sid}})
                    if ch == "orderbook_delta":
                        for tk in p.get("market_tickers", []):
                            await self.snapshot(ws, tk)
            elif d["cmd"] == "update_subscription":
                sub = next(s for s in self.subs[ws].values() if s["sid"] == p["sid"])
                ch = next(c for c, s in self.subs[ws].items() if s is sub)
                if p["action"] == "add_markets":
                    sub["tickers"] |= set(p["market_tickers"])
                    await ws.send_json({"id": d["id"], "type": "ok", "sid": p["sid"],
                                        "msg": {"market_tickers": sorted(sub["tickers"])}})
                    if ch == "orderbook_delta":
                        for tk in p["market_tickers"]:
                            await self.snapshot(ws, tk)
                elif p["action"] == "delete_markets":
                    sub["tickers"] -= set(p["market_tickers"])
                    await ws.send_json({"id": d["id"], "type": "ok", "sid": p["sid"], "msg": {}})
        self.clients.remove(ws)
        return ws

    def _seq(self, ws, sid):
        self.seq[(id(ws), sid)] = self.seq.get((id(ws), sid), 0) + 1
        return self.seq[(id(ws), sid)]

    async def snapshot(self, ws, tk):
        sub = self.subs[ws]["orderbook_delta"]
        yes_leg = sub["p"].get("use_yes_price")
        ask = "0.9930" if yes_leg else "0.0070"                        # NO bid 0.7c = YES ask 99.3c
        await ws.send_json({"type": "orderbook_snapshot", "sid": sub["sid"], "seq": self._seq(ws, sub["sid"]),
                            "sending_ts_ms": int(time.time() * 1000),
                            "msg": {"market_ticker": tk, "yes_dollars_fp": [["0.9900", "80.00"], ["0.9920", "500.00"]],
                                    "no_dollars_fp": [[ask, "400.00"]]}})

    async def pump(self):
        i = 0
        while True:
            await asyncio.sleep(0.25)
            i += 1
            now_ms = int(time.time() * 1000)
            for ws in list(self.clients):
                if ws.closed:
                    continue
                subs = self.subs.get(ws, {})
                ob, tr, cf = subs.get("orderbook_delta"), subs.get("trade"), subs.get("cfbenchmarks_value")
                try:
                    if ob:
                        for tk in ob["tickers"]:
                            await ws.send_json({"type": "orderbook_delta", "sid": ob["sid"], "seq": self._seq(ws, ob["sid"]),
                                                "sending_ts_ms": now_ms, "msg": {
                                                    "market_ticker": tk, "price_dollars": "0.9920",
                                                    "delta_fp": "1.00" if i % 2 else "-1.00", "side": "yes", "ts_ms": now_ms}})
                    if tr and i % 2 == 0:
                        for tk in tr["tickers"]:
                            await ws.send_json({"type": "trade", "sid": tr["sid"], "seq": i, "sending_ts_ms": now_ms, "msg": {
                                "trade_id": f"t{i}", "market_ticker": tk, "yes_price_dollars": "0.9920",
                                "no_price_dollars": "0.0080", "count_fp": "300.00", "taker_side": "no", "ts_ms": now_ms}})
                    if cf and i % 4 == 0:
                        for iid in cf["p"].get("index_ids", []):
                            await ws.send_json({"type": "cfbenchmarks_value", "sid": cf["sid"], "seq": i, "msg": {
                                "index_id": iid, "received_at": now_ms,
                                "data": json.dumps({"type": "value", "id": iid, "time": now_ms, "value": "70010.5"}),
                                "avg_60s_data": {"value": "70008.00000000", "window_size": 40},
                                "last_60s_windowed_average_15min": {"value": "70009.00000000", "window_size": 25}}})
                except ConnectionResetError:
                    pass


class Signer:
    def headers(self, key_id, method, path):
        return {"KALSHI-ACCESS-KEY": key_id, "KALSHI-ACCESS-TIMESTAMP": "0", "KALSHI-ACCESS-SIGNATURE": "x"}


async def main(serve: bool) -> int:
    fake = FakeKalshi()
    srv = web.Application()
    srv.add_routes([web.get("/trade-api/ws/v2", fake.ws)])
    r1 = web.AppRunner(srv, access_log=None)
    await r1.setup()
    await web.TCPSite(r1, "127.0.0.1", 18765).start()
    pump = asyncio.ensure_future(fake.pump())

    cfg = C.load()
    cfg.update(source="ws", ws_url="ws://127.0.0.1:18765/trade-api/ws/v2")
    cfg["markets"] = {"crypto": {k: cfg["markets"]["crypto"][k] for k in ("BTC", "ETH")},
                      "commodity": {"GOLD": cfg["markets"]["commodity"]["GOLD"]}}
    cfg["news"]["skip"] = False
    cfg["record"]["pyth"] = []
    app = A.App(cfg)
    c = app.client
    c.signer, c.key_id, c.key_error = Signer(), "smoke", None
    c.open_market, c.market, c.book = fake.open_market, fake.market_by_ticker, fake.book
    for m in app.engine.markets.values():
        m.next_result_check = 0
    C.set_login(username="admin", password="smoke-test-pw")
    cfg["web"]["password_hash"] = C.load()["web"]["password_hash"]
    r2 = web.AppRunner(make_app(app), access_log=None)
    await r2.setup()
    await web.TCPSite(r2, "127.0.0.1", 18766).start()
    run = asyncio.ensure_future(app.run())
    print("fake exchange up; program running at http://127.0.0.1:18766  (sign in: admin / smoke-test-pw)")
    if serve:
        await run
        return 0
    await asyncio.sleep(3)
    for m in app.engine.markets.values():
        m.next_result_check = m.close_ts + 2                       # check results quickly in the test
    await asyncio.sleep(40)
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.get("http://127.0.0.1:18766/api/status", headers={"X-Token": cfg["web"]["token"]}) as r:
            st = await r.json()
    tr = app.store.trades(limit=50)
    checks = {
        "WebSocket connected": app.ws.connected,
        "both market channels subscribed": {"orderbook_delta", "trade"} <= set(app.ws.sids),
        "index channel subscribed": "cfbenchmarks_value" in app.ws.sids,
        "no WebSocket errors": not app.ws.errors and not app.ws.unknown,
        "no sequence gaps": app.ws.gaps == 0,
        "second window opened after the first closed": any(m["ticker"].endswith("-W1") for m in st["markets"]),
        "books live on the new window": all(m["bid"] is not None for m in st["markets"] if m["ticker"].endswith("-W1")),
        "clock offset measured (near zero here)": abs(app.offset) < 0.5,
        "index value received": "BRTI" in app.engine.index and app.engine.index["BRTI"]["win_n"] == 25,
        "paper trade opened by crypto_99": any(t["rule"] == "crypto_99" for t in tr),
        "paper trade settled as won": any(t["rule"] == "crypto_99" and t["exit"] == "won" and t["pnl_c"] > 0 for t in tr),
        "no live trades": all(t["mode"] == "paper" for t in tr),
        "recorder wrote rows": app.rec.rows > 50,
        "status API lists rules": len(st["scoreboard"]) == 5,
    }
    for k, v in checks.items():
        print(("  ok   " if v else "  FAIL ") + k)
    if not all(checks.values()):
        print("ws status:", app.ws.status())
        print("events:", [e["msg"] for e in app.store.events(15)])
    app.stopping = True
    app.ws.stop = True
    run.cancel()
    pump.cancel()
    print("PASS" if all(checks.values()) else "FAIL")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main("--serve" in sys.argv)))
    except KeyboardInterrupt:
        pass
