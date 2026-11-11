"""Kalshi WebSocket client: signed connection, subscriptions, reconnects.

Written from Kalshi's published docs (Nov 2026). It could not be tested against the live exchange when it was
written, so it is defensive: every control message is logged, unknown message types are counted rather than
dropped silently, and the app cross-checks the WebSocket book against the REST book (see App.audit).

  URL            wss://api.elections.kalshi.com/trade-api/ws/v2
  Auth           the REST signing headers, signing  <timestamp_ms> + "GET" + "/trade-api/ws/v2"
  Subscribe      {"id": 1, "cmd": "subscribe", "params": {"channels": [...], "market_tickers": [...]}}
  Add markets    {"id": 2, "cmd": "update_subscription",
                  "params": {"sid": 5, "action": "add_markets", "market_tickers": [...]}}
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque

import aiohttp

MARKET_CHANNELS = ("orderbook_delta", "trade")


class KalshiWS:
    def __init__(self, url: str, signer, key_id: str, sign_path: str, on_data, on_event, use_yes_price: bool = True,
                 on_raw=None):
        self.url, self.signer, self.key_id, self.sign_path = url, signer, key_id, sign_path
        self.on_data, self.on_event, self.on_raw = on_data, on_event, on_raw
        self.use_yes_price = use_yes_price
        self.tickers: set[str] = set()
        self.extra: dict[str, dict] = {}          # channel -> params for channels not tied to markets
        self.sids: dict[str, int] = {}
        self.pending: dict[int, tuple[str, str]] = {}
        self.seq: dict[int, int] = {}
        self.ws = None
        self._id = 0
        self.connected = False
        self.stop = False
        self.msgs = 0
        self.last_msg = 0.0
        self.reconnects = 0
        self.gaps = 0
        self.unknown: dict[str, int] = {}
        self.errors: deque = deque(maxlen=20)

    # ---- commands
    async def _send(self, cmd: str, params: dict, tag: tuple[str, str]):
        if not self.ws or self.ws.closed:
            return
        self._id += 1
        self.pending[self._id] = tag
        await self.ws.send_str(json.dumps({"id": self._id, "cmd": cmd, "params": params}))

    async def _subscribe_markets(self, channel: str):
        if not self.tickers:
            return
        p = {"channels": [channel], "market_tickers": sorted(self.tickers)}
        if channel == "orderbook_delta":
            p["use_yes_price"] = self.use_yes_price
        await self._send("subscribe", p, (channel, "subscribe"))

    async def _subscribe_all(self):
        for ch in MARKET_CHANNELS:
            await self._subscribe_markets(ch)
        for ch, params in self.extra.items():
            await self._send("subscribe", {"channels": [ch], **params}, (ch, "subscribe"))

    def want(self, channel: str, params: dict):
        """A channel that isn't tied to markets (CF Benchmarks index values, Pyth values)."""
        self.extra[channel] = params

    async def set_markets(self, tickers):
        new = set(tickers)
        add, rem = new - self.tickers, self.tickers - new
        self.tickers = new
        if not self.connected or (not add and not rem):
            return
        for ch in MARKET_CHANNELS:
            sid = self.sids.get(ch)
            if sid is None:
                await self._subscribe_markets(ch)
                continue
            if add:
                p = {"sid": sid, "action": "add_markets", "market_tickers": sorted(add)}
                if ch == "orderbook_delta":
                    p["send_initial_snapshot"] = True
                await self._send("update_subscription", p, (ch, "update"))
            if rem:
                await self._send("update_subscription",
                                 {"sid": sid, "action": "delete_markets", "market_tickers": sorted(rem)}, (ch, "update"))

    async def reconnect(self, why: str):
        self.on_event("warn", f"WebSocket reconnecting: {why}")
        if self.ws and not self.ws.closed:
            await self.ws.close()

    # ---- the connection
    async def run(self):
        backoff = 1.0
        async with aiohttp.ClientSession() as session:
            while not self.stop:
                try:
                    hdr = self.signer.headers(self.key_id, "GET", self.sign_path)
                    async with session.ws_connect(self.url, headers=hdr, heartbeat=15, max_msg_size=0) as ws:
                        self.ws, self.connected = ws, True
                        self.sids.clear()
                        self.seq.clear()
                        self.pending.clear()
                        self.on_event("info", "WebSocket connected")
                        backoff = 1.0
                        await self._subscribe_all()
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self._handle(msg.data, time.time())
                            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                                break
                except asyncio.CancelledError:
                    raise
                except Exception as e:                                # noqa: BLE001
                    self.errors.append((time.time(), f"{type(e).__name__}: {e}"))
                    self.on_event("warn", f"WebSocket error: {type(e).__name__}: {str(e)[:200]}")
                finally:
                    self.connected, self.ws = False, None
                if self.stop:
                    break
                self.reconnects += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _handle(self, text: str, recv: float):
        self.msgs += 1
        self.last_msg = recv
        if self.on_raw:
            self.on_raw(recv, text)
        try:
            d = json.loads(text)
        except ValueError:
            return
        typ = d.get("type")
        if typ == "subscribed":
            m = d.get("msg") or {}
            ch = m.get("channel") or (self.pending.get(d.get("id"), ("", ""))[0])
            sid = m.get("sid", d.get("sid"))
            if ch and sid is not None:
                self.sids[ch] = sid
            self.pending.pop(d.get("id"), None)
            self.on_event("info", f"subscribed to {ch} (sid {sid})")
            return
        if typ in ("ok", "unsubscribed"):
            self.pending.pop(d.get("id"), None)
            return
        if typ == "error":
            ch, kind = self.pending.pop(d.get("id"), ("?", "?"))
            m = d.get("msg") or {}
            self.errors.append((recv, f"{ch} {kind}: code {m.get('code')} {m.get('msg')}"))
            self.on_event("error", f"WebSocket {kind} on {ch} refused: code {m.get('code')} {m.get('msg')}")
            if kind == "update" and self.ws and not self.ws.closed:      # start clean rather than guess the state
                asyncio.ensure_future(self.reconnect(f"update on {ch} refused"))
            return
        sid, seq = d.get("sid"), d.get("seq")
        if sid is not None and seq is not None and typ in ("orderbook_snapshot", "orderbook_delta"):
            last = self.seq.get(sid)
            self.seq[sid] = seq
            if last is not None and seq != last + 1:
                self.gaps += 1
                asyncio.ensure_future(self.reconnect(f"missed order-book messages (seq {last} -> {seq})"))
                return
        try:
            handled = self.on_data(typ, d.get("msg") or {}, recv, d.get("sending_ts_ms"))
        except Exception as e:                                        # noqa: BLE001
            handled = True
            self.errors.append((recv, f"handler {typ}: {type(e).__name__}: {e}"))
        if not handled:
            self.unknown[str(typ)] = self.unknown.get(str(typ), 0) + 1

    def status(self) -> dict:
        return {"connected": self.connected, "msgs": self.msgs, "age": round(time.time() - self.last_msg, 1) if
                self.last_msg else None, "reconnects": self.reconnects, "gaps": self.gaps, "sids": dict(self.sids),
                "unknown": dict(self.unknown), "errors": [f"{time.strftime('%H:%M:%S', time.localtime(t))} {e}"
                                                          for t, e in list(self.errors)[-5:]]}
