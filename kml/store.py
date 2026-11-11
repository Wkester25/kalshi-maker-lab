"""SQLite store: every trade (paper and live), every order that did not fill, every market and its result.

Unfilled orders are kept on purpose. In the review, bids that saw a print at their price but did not fill won
99%, while filled bids won 95.8%. Comparing the two is the first check to run on any rule.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades(
  id INTEGER PRIMARY KEY, rule TEXT, mode TEXT, ticker TEXT, coin TEXT, grp TEXT, close_ts REAL,
  side TEXT, px REAL, n REAL, kind TEXT, t_place REAL, t_fill REAL, secs REAL, fee_c REAL DEFAULT 0,
  exit TEXT, exit_px REAL, t_exit REAL, pnl_c REAL, result TEXT, feat TEXT);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY, rule TEXT, mode TEXT, ticker TEXT, coin TEXT, close_ts REAL, side TEXT, px REAL,
  n REAL, t_place REAL, t_end REAL, secs REAL, outcome TEXT, queue REAL, at_px_vol REAL, won INTEGER);
CREATE TABLE IF NOT EXISTS markets(
  ticker TEXT PRIMARY KEY, coin TEXT, grp TEXT, open_ts REAL, close_ts REAL, strike REAL, result TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, t REAL, level TEXT, msg TEXT);
CREATE INDEX IF NOT EXISTS trades_rule ON trades(rule, mode);
CREATE INDEX IF NOT EXISTS trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS orders_ticker ON orders(ticker);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    def _x(self, sql: str, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def _q(self, sql: str, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    # ---- writes
    def event(self, level: str, msg: str, t: float | None = None):
        self._x("INSERT INTO events(t, level, msg) VALUES(?,?,?)", (t or time.time(), level, msg[:500]))

    def market(self, m):
        self._x("INSERT OR IGNORE INTO markets(ticker, coin, grp, open_ts, close_ts, strike) VALUES(?,?,?,?,?,?)",
                (m.ticker, m.coin, m.group, m.open_ts, m.close_ts, m.strike))

    def open_trade(self, **k) -> int:
        k["feat"] = json.dumps(k.get("feat") or {})
        cols = ",".join(k)
        return self._x(f"INSERT INTO trades({cols}) VALUES({','.join('?' * len(k))})", tuple(k.values())).lastrowid

    def close_trade(self, tid: int, exit: str, pnl_c: float, result: str | None = None, exit_px: float | None = None,
                    t_exit: float | None = None, fee_c: float | None = None):
        self._x("UPDATE trades SET exit=?, pnl_c=?, result=COALESCE(?, result), exit_px=?, t_exit=?, "
                "fee_c=COALESCE(?, fee_c) WHERE id=?", (exit, pnl_c, result, exit_px, t_exit, fee_c, tid))

    def order(self, **k):
        cols = ",".join(k)
        self._x(f"INSERT INTO orders({cols}) VALUES({','.join('?' * len(k))})", tuple(k.values()))

    def result(self, ticker: str, result: str):
        """result is 'yes' or 'no'. Marks unfilled orders with whether their side went on to win."""
        self._x("UPDATE markets SET result=? WHERE ticker=?", (result, ticker))
        self._x("UPDATE orders SET won = CASE WHEN (side='YES') = (?='yes') THEN 1 ELSE 0 END WHERE ticker=?",
                (result, ticker))
        self._x("UPDATE trades SET result=? WHERE ticker=? AND result IS NULL", (result, ticker))

    # ---- reads
    def events(self, limit: int = 200):
        return self._q("SELECT t, level, msg FROM events ORDER BY id DESC LIMIT ?", (limit,))

    def trades(self, rule: str | None = None, limit: int = 200):
        if rule:
            return self._q("SELECT * FROM trades WHERE rule=? ORDER BY id DESC LIMIT ?", (rule, limit))
        return self._q("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))

    def open_trades(self):
        return self._q("SELECT * FROM trades WHERE exit IS NULL")

    def closed(self, rule: str, mode: str, since: float = 0.0):
        return self._q("SELECT close_ts, pnl_c, n, px, exit, coin, t_fill FROM trades WHERE rule=? AND mode=? AND "
                       "exit IS NOT NULL AND t_fill>=? ORDER BY t_fill", (rule, mode, since))

    def day_pnl_c(self, mode: str, since: float) -> float:
        r = self._q("SELECT COALESCE(SUM(pnl_c),0) s FROM trades WHERE mode=? AND exit IS NOT NULL AND t_fill>=?",
                    (mode, since))
        return float(r[0]["s"])

    def unfilled(self, rule: str, mode: str):
        return self._q("SELECT outcome, COUNT(*) n, AVG(won) won, AVG(px) px FROM orders WHERE rule=? AND mode=? "
                       "AND won IS NOT NULL GROUP BY outcome", (rule, mode))

    def scoreboard(self, rule: str, mode: str, since: float = 0.0) -> dict:
        return stats(self.closed(rule, mode, since))


def stats(rows: list[dict]) -> dict:
    """Per-contract results with a standard error clustered by 15-minute slot (markets in a slot move together)."""
    n = len(rows)
    if not n:
        return {"n": 0}
    per = [r["pnl_c"] / max(r["n"], 1e-9) for r in rows]             # cents per contract, one number per trade
    mean = sum(per) / n
    slots: dict[float, list[float]] = {}
    for r, p in zip(rows, per):
        slots.setdefault(round(r["close_ts"]), []).append(p)
    g = len(slots)
    se = None
    if g > 1:
        ss = sum((sum(v) - mean * len(v)) ** 2 for v in slots.values())
        se = math.sqrt(ss * g / (g - 1)) / n
    wins = [p for p in per if p > 0]
    bad = [p for p in per if p <= 0]
    out = {"n": n, "slots": g, "mean_c": mean, "se_c": se, "total_c": sum(r["pnl_c"] for r in rows),
           "wins": len(wins), "bad": len(bad), "win_rate": len(wins) / n,
           "avg_win_c": sum(wins) / len(wins) if wins else None, "avg_bad_c": sum(bad) / len(bad) if bad else None,
           "avg_px": sum(r["px"] for r in rows) / n, "stopped": sum(1 for r in rows if r["exit"] == "stopped")}
    if se:
        out["t"] = mean / se
        out["lo95_c"], out["hi95_c"] = mean - 1.96 * se, mean + 1.96 * se
    return out


def ladder(s: dict) -> dict:
    """The size the evidence supports, from cumulative results since launch (never reset). From the review:
    2 contracts need 1,000 trades and a mean 1 standard error above zero; 3 need 2,500 and 1.5; 5 need 5,000 and 2."""
    n, t = s.get("n", 0), s.get("t")
    size, why = 1, "fewer than 1,000 trades"
    if t is not None:
        for need_n, need_t, sz in ((5000, 2.0, 5), (2500, 1.5, 3), (1000, 1.0, 2)):
            if n >= need_n and t >= need_t:
                size, why = sz, f"{n} trades, mean {t:.1f} standard errors above zero"
                break
        else:
            if n >= 1000:
                why = f"{n} trades but the mean is only {t:.1f} standard errors from zero"
        if n >= 2500 and s["mean_c"] < 0:
            size, why = 0, f"mean is negative after {n} trades: switch the rule off"
    return {"contracts": size, "why": why}
