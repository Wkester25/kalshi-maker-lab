"""Writes market data to disk with the time each message ARRIVED, so later tests can replay exactly what
the program could have known and when. Hourly CSV files, gzipped once the hour is over.

  top_*.csv     best YES bid/ask and sizes, one row per change      (mode "top", the default)
  raw_*.jsonl   every WebSocket message as received                  (mode "full": large, complete)
  trades_*.csv  every public trade
  index_*.csv   CF Benchmarks index values, including the 60-second settlement average in the final minute
  pyth_*.csv    Pyth values (what commodity markets settle on)
  markets_*.csv each window's ticker, times, strike and result
"""
from __future__ import annotations

import gzip
import json
import shutil
import threading
import time
from pathlib import Path

HEAD = {
    "top": "recv_ms,exch_ms,ticker,yes_bid,yes_ask,bid_size,ask_size",
    "trades": "recv_ms,ts_ms,ticker,yes_price,count,taker_side",
    "index": "recv_ms,index_id,value,src_ms,avg60,avg60_n,settle_avg,settle_n",
    "pyth": "recv_ms,underlying,value,src_ms",
    "markets": "recv_ms,ticker,coin,open_ts,close_ts,strike,result",
}


class Recorder:
    def __init__(self, cfg: dict, root: Path):
        r = cfg["record"]
        self.on, self.mode, self.gz = bool(r.get("enabled")), r.get("mode", "top"), bool(r.get("gzip", True))
        self.dir = (root / r.get("dir", "data/rec")).resolve()
        self.files: dict[str, tuple] = {}
        self.last_top: dict[str, tuple] = {}
        self.rows = 0
        self.lock = threading.Lock()
        if self.on:
            self.dir.mkdir(parents=True, exist_ok=True)

    def _fh(self, kind: str):
        stamp = time.strftime("%Y-%m-%d_%H", time.gmtime())
        cur = self.files.get(kind)
        if cur and cur[0] == stamp:
            return cur[1]
        if cur:
            cur[1].close()
            if self.gz:
                threading.Thread(target=self._zip, args=(cur[2],), daemon=True).start()
        ext = "jsonl" if kind == "raw" else "csv"
        path = self.dir / stamp[:10] / f"{kind}_{stamp}.{ext}"
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        fh = path.open("a", encoding="utf-8", newline="")
        if new and kind in HEAD:
            fh.write(HEAD[kind] + "\n")
        self.files[kind] = (stamp, fh, path)
        return fh

    @staticmethod
    def _zip(path: Path):
        try:
            with path.open("rb") as src, gzip.open(str(path) + ".gz", "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst)
            path.unlink()
        except OSError:
            pass

    def _w(self, kind: str, line: str):
        if not self.on:
            return
        with self.lock:
            self._fh(kind).write(line + "\n")
            self.rows += 1

    def raw(self, recv: float, text: str):
        if self.on and self.mode == "full":
            self._w("raw", f"[{recv:.4f},{text}]")

    def top(self, recv: float, ticker: str, book):
        bb, bs, ba, as_ = book.top()
        key = (bb, ba, bs, as_)
        if self.last_top.get(ticker) == key:
            return
        self.last_top[ticker] = key
        self._w("top", f"{recv * 1000:.0f},{book.exch_ms},{ticker},{'' if bb is None else bb / 10:g},"
                       f"{'' if ba is None else ba / 10:g},{bs:g},{as_:g}")

    def trade(self, recv: float, ts_ms, ticker: str, yes_c: float, count: float, taker_side: str):
        self._w("trades", f"{recv * 1000:.0f},{ts_ms},{ticker},{yes_c:g},{count:g},{taker_side}")

    def index(self, recv: float, index_id: str, value, src_ms, a60, a60n, win, winn):
        f = lambda x: "" if x is None else f"{x}"                        # noqa: E731
        self._w("index", f"{recv * 1000:.0f},{index_id},{f(value)},{f(src_ms)},{f(a60)},{f(a60n)},{f(win)},{f(winn)}")

    def pyth(self, recv: float, underlying: str, value, src_ms):
        self._w("pyth", f"{recv * 1000:.0f},{underlying},{value},{src_ms}")

    def market(self, recv: float, m, result: str = ""):
        self._w("markets", f"{recv * 1000:.0f},{m.ticker},{m.coin},{m.open_ts:.0f},{m.close_ts:.0f},"
                           f"{'' if m.strike is None else m.strike},{result}")

    def flush(self):
        with self.lock:
            for _, fh, _ in self.files.values():
                try:
                    fh.flush()
                except OSError:
                    pass

    def close(self):
        with self.lock:
            for _, fh, _ in self.files.values():
                try:
                    fh.close()
                except OSError:
                    pass
            self.files.clear()
