#!/usr/bin/env python3
"""Standard read-only report: health, then results per rule with slot-clustered error bars.

  python tools/analyze.py                  everything since the scoring start ("score_since" in kml/config.py)
  python tools/analyze.py --days 3         only trades filled in the last 3 days
  python tools/analyze.py --all            include data from before the scoring start
  python tools/analyze.py --db other.db

Safe to run while the program is running. Prints markdown.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from kml import config as C                   # noqa: E402
from kml.store import ladder, stats          # noqa: E402

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                            # noqa: BLE001
    ET = timezone(timedelta(hours=-5))


def c(x, nd=2):
    return "-" if x is None else f"{x:+.{nd}f}c"


def rng(s):
    return f"{s['lo95_c']:+.2f} to {s['hi95_c']:+.2f}" if s.get("se_c") else "-"


def wilson_hi(k: int, n: int, z: float = 1.96) -> float:
    """95% upper bound on a rate after k events in n tries (works for k = 0)."""
    if n == 0:
        return 1.0
    p = k / n
    return min(1.0, (p + z * z / (2 * n) + z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5) / (1 + z * z / n))


def table(head, rows):
    out = ["| " + " | ".join(head) + " |", "|" + " --- |" * len(head)]
    out += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "lab.db"))
    ap.add_argument("--days", type=float, default=0)
    ap.add_argument("--all", action="store_true", help="include data from before the scoring start")
    a = ap.parse_args()
    if not Path(a.db).exists():
        print(f"No database at {a.db}: the program hasn't recorded anything yet.")
        return 1
    db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    q = lambda sql, args=(): [dict(r) for r in db.execute(sql, args).fetchall()]   # noqa: E731
    now = time.time()
    since = now - a.days * 86400 if a.days else 0.0
    over = {}
    try:                                     # read-only: config.json may override score_since
        over = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    start = 0.0 if a.all else C.score_since({**C.DEFAULTS, **{k: v for k, v in over.items() if k == "score_since"}})
    since = max(since, start)
    what = "all data" if not since else f"from {datetime.fromtimestamp(since, ET):%m-%d %H:%M} ET"
    if start and since == start:
        what += " (the scoring start; --all for earlier data)"
    L = [f"# Kalshi Maker Lab analysis, {datetime.fromtimestamp(now, ET):%Y-%m-%d %H:%M} ET",
         f"Window: {what}. Cents per contract. 95% ranges use standard errors clustered by 15-minute slot.", ""]

    # ---------------- health
    L += ["## Health", ""]
    first = q("SELECT MIN(t) t FROM events")[0]["t"]
    last_ev = q("SELECT MAX(t) t FROM events")[0]["t"]
    mk = q("SELECT COUNT(*) n, SUM(result IS NOT NULL) settled, MAX(close_ts) last FROM markets")[0]
    L.append(f"- Running since {datetime.fromtimestamp(first, ET):%m-%d %H:%M} ET; last event "
             f"{(now - last_ev) / 60:.0f} min ago." if first else "- No events logged yet.")
    L.append(f"- Markets seen {mk['n']}, settled {mk['settled'] or 0}; latest window closes "
             f"{datetime.fromtimestamp(mk['last'], ET):%m-%d %H:%M} ET." if mk["n"] else "- No markets yet.")
    if mk["n"] and mk["last"] and now - mk["last"] > 1800:
        L.append("- **WARNING: no new market in 30 minutes. The program may be stopped or the feed down.**")
    lv = q("SELECT level, COUNT(*) n FROM events WHERE t>=? GROUP BY level", (since,))
    L.append("- Events: " + ", ".join(f"{r['level']} {r['n']}" for r in lv))
    audit = q("SELECT COUNT(*) n FROM events WHERE msg LIKE '%disagrees with the REST book%' AND t>=?", (since,))[0]["n"]
    L.append(f"- WebSocket vs REST book disagreements logged: {audit}" + ("  **(book parsing suspect: see docs/HISTORY.md, open question 2)**" if audit else ""))
    halts = q("SELECT t, msg FROM events WHERE level='halt' ORDER BY t DESC LIMIT 3")
    for h in halts:
        L.append(f"- halt {datetime.fromtimestamp(h['t'], ET):%m-%d %H:%M}: {h['msg']}")
    errs = q("SELECT msg, COUNT(*) n FROM events WHERE level='error' AND t>=? GROUP BY substr(msg,1,60) "
             "ORDER BY n DESC LIMIT 6", (since,))
    if errs:
        L += ["- Most common errors:"] + [f"  - {e['n']}x {e['msg'][:140]}" for e in errs]
    rec = sorted(glob.glob(str(ROOT / "data" / "rec" / "*" / "*")))
    if rec:
        newest = max(rec, key=os.path.getmtime)
        size = sum(os.path.getsize(f) for f in rec) / 1e6
        L.append(f"- Recordings: {len(rec)} files, {size:.0f} MB; newest written "
                 f"{(now - os.path.getmtime(newest)) / 60:.0f} min ago ({Path(newest).name}).")
    else:
        L.append("- Recordings: none found in data/rec/.")
    L.append("")

    # ---------------- results
    has_restart = any(r["name"] == "restart" for r in q("PRAGMA table_info(trades)"))
    rows = q("SELECT rule, mode, close_ts, pnl_c, n, px, exit, coin, t_fill, side, secs, "
             f"{'restart' if has_restart else '0 AS restart'} FROM trades "
             "WHERE exit IS NOT NULL AND exit != 'unresolved' AND t_fill>=? ORDER BY t_fill", (since,))
    open_n = q("SELECT COUNT(*) n FROM trades WHERE exit IS NULL AND t_fill>=?", (since,))[0]["n"]
    unres = q("SELECT rule, COUNT(*) n FROM trades WHERE exit='unresolved' AND t_fill>=? GROUP BY rule", (since,))
    by = defaultdict(list)
    for r in rows:
        by[(r["rule"], r["mode"])].append(r)
    L += ["## Results by rule", ""]
    if not rows:
        L.append(f"No settled trades yet ({open_n} open).")
    else:
        tr = []
        for (rule, mode), rr in sorted(by.items()):
            s = stats(rr)
            tr.append([rule, mode, s["n"], s["slots"], c(s["mean_c"]), rng(s), f"{s['win_rate']:.2%}", s["bad"],
                       s["stopped"], f"{s['avg_px']:.2f}", f"{s['total_c'] / 100:+.2f}", ladder(s)["contracts"]])
        L.append(table(["Rule", "Mode", "Trades", "Slots", "Mean", "95% range", "Won", "Bad", "Stopped", "Avg entry",
                        "Total $", "Size supported"], tr))
        L += ["", f"{open_n} positions still open."]
        if unres:
            L.append("Unresolved (no market result, left out above): " +
                     ", ".join(f"{u['rule']} {u['n']}" for u in unres) + ".")
        rs = [(k, rr) for k, rr in sorted(by.items()) if any(r["restart"] for r in rr)]
        if rs:
            L += ["", "Positions open across a restart are held to settlement and counted above. Without them:"]
            for (rule, mode), rr in rs:
                k = [r for r in rr if not r["restart"]]
                L.append(f"- {rule} ({mode}): {sum(1 for r in rr if r['restart'])} restart-affected; "
                         f"{c(stats(k)['mean_c']) if k else '-'} on the other {len(k)}")
        L += ["",
              "## Loss rate against break-even", "",
              "Wins are small and losses large, so the mean's range understates the risk while losses are rare. "
              "This compares the loss rate with the rate the rule can afford.", ""]
        lr = []
        for (rule, mode), rr in sorted(by.items()):
            s = stats(rr)
            avg_win = s["avg_win_c"] or 0.0
            avg_bad = s["avg_bad_c"] if s["avg_bad_c"] is not None else -s["avg_px"]      # none yet: assume a total loss
            be = avg_win / (avg_win - avg_bad) if avg_win > 0 else 0.0
            hi = wilson_hi(s["bad"], s["n"])
            need = int(3 / be) + 1 if be > 0 else None
            verdict = ("loss rate is clearly below break-even" if hi < be else
                       "not enough trades to tell" if s["bad"] / s["n"] < be else "losing more often than it can afford")
            lr.append([rule, mode, s["n"], s["bad"], f"{s['bad'] / s['n']:.2%}", f"{hi:.2%}", f"{be:.2%}",
                       need if need and s["bad"] == 0 else "-", verdict])
        L.append(table(["Rule", "Mode", "Trades", "Bad", "Bad rate", "95% upper bound", "Break-even rate",
                        "Trades needed if none go bad", "Verdict"], lr))
        L.append("")

        L += ["## By day (ET)", ""]
        for (rule, mode), rr in sorted(by.items()):
            d = defaultdict(list)
            for r in rr:
                d[datetime.fromtimestamp(r["close_ts"], ET).strftime("%m-%d %a")].append(r)
            L.append(f"- **{rule}** ({mode}): " + ", ".join(
                f"{k} {c(stats(v)['mean_c'])} ({len(v)}, {stats(v)['bad']} bad)" for k, v in sorted(d.items())))
        L += ["", "## By market", ""]
        for (rule, mode), rr in sorted(by.items()):
            d = defaultdict(list)
            for r in rr:
                d[r["coin"]].append(r)
            L.append(f"- **{rule}** ({mode}): " + ", ".join(
                f"{k} {c(stats(v)['mean_c'])} ({len(v)}, {stats(v)['bad']} bad)" for k, v in sorted(d.items())))
        L += ["", "## By minute of the window (fill time)", ""]
        for (rule, mode), rr in sorted(by.items()):
            d = defaultdict(list)
            for r in rr:
                d[int((r["secs"] or 0) // 60)].append(r)
            L.append(f"- **{rule}** ({mode}): " + ", ".join(
                f"{k}:00 {c(stats(v)['mean_c'])} ({len(v)})" for k, v in sorted(d.items())))
        L.append("")

    # ---------------- filled vs unfilled
    L += ["## Filled against unfilled bids", "",
          "Unfilled bids winning clearly more often than filled ones means fills are selected against the rule.", ""]
    fu = []
    for (rule, mode), rr in sorted(by.items()):
        u = q("SELECT COUNT(*) n, AVG(won) w, SUM(at_px_vol>0) printed, AVG(CASE WHEN at_px_vol>0 THEN won END) wp "
              "FROM orders WHERE rule=? AND mode=? AND won IS NOT NULL AND t_place>=?", (rule, mode, since))[0]
        filled_won = sum(1 for r in rr if r["exit"] == "won") / len(rr)
        fu.append([rule, mode, len(rr), f"{filled_won:.2%}", u["n"], "-" if u["w"] is None else f"{u['w']:.2%}",
                   u["printed"] or 0, "-" if u["wp"] is None else f"{u['wp']:.2%}"])
    L.append(table(["Rule", "Mode", "Filled", "Filled won", "Unfilled", "Unfilled would have won",
                    "Unfilled with a print at our price", "Those would have won"], fu) if fu else "No data yet.")

    # ---------------- feature split on fills (exploratory; do not act on it without fresh data)
    feats = q("SELECT rule, mode, pnl_c, n, exit, feat FROM trades WHERE exit IS NOT NULL AND exit!='unresolved' "
              "AND t_fill>=?", (since,))
    if len(feats) >= 200:
        L += ["", "## Exploratory: results by book imbalance at the fill", "",
              "Our side's share of top-of-book size. Exploratory only: a split found here must hold on later data "
              "before it becomes a rule.", ""]
        d = defaultdict(list)
        for r in feats:
            try:
                imb = json.loads(r["feat"] or "{}").get("imb")
            except ValueError:
                imb = None
            if imb is None:
                continue
            b = "0-20%" if imb <= .2 else "20-50%" if imb <= .5 else "50-80%" if imb <= .8 else "80-100%"
            d[(r["rule"], b)].append({**r, "close_ts": 0, "px": 0})
        L.append(", ".join(f"{k[0]} {k[1]}: {sum(x['pnl_c'] / x['n'] for x in v) / len(v):+.2f}c ({len(v)})"
                           for k, v in sorted(d.items())))
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    sys.exit(main())
