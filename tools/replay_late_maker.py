#!/usr/bin/env python3
"""Score the rules on the Late Maker bot's old recordings, using the real engine in paper mode.

  python tools/replay_late_maker.py "C:\\Users\\you\\Documents\\Kalshi Late Maker Bot\\data"
  python tools/replay_late_maker.py <data folder> --coins GOLD,SILVER --shift 1.2

Reads book_<COIN>_<date>.csv, results.csv and every trades file it finds (trades.csv, trades_parts/*.csv,
review/*.csv). One coin at a time, to keep memory down; a full run over 3,000 windows takes several minutes.
--shift is how far behind Kalshi the recording PC's clock was (measured at 1.2 s for the Nov 8-10 recordings).
"""
from __future__ import annotations

import argparse
import copy
import csv
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kml import config as C                      # noqa: E402
from kml.engine import Engine, Market            # noqa: E402
from kml.replay import Clock, replay_window      # noqa: E402
from kml.store import Store                      # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("--coins", default="")
    ap.add_argument("--shift", type=float, default=1.2)
    ap.add_argument("--latency-ms", type=int, default=300)
    a = ap.parse_args()
    data = Path(a.data)
    with (data / "results.csv").open(encoding="utf-8") as fh:
        res = {r["ticker"]: r["result"] for r in csv.DictReader(fh) if r.get("result") in ("UP", "DOWN")}
    trade_files = [p for p in [data / "trades.csv", *sorted((data / "review").glob("trades_*.csv"))] if p.exists()]
    if not (data / "trades.csv").exists():
        trade_files += sorted((data / "trades_parts").glob("trades_*.csv"))
    cfg = copy.deepcopy(C.DEFAULTS)
    cfg["rules"] = [{**C.RULE_DEFAULTS, **r} for r in C.RULES]
    cfg["paper"].update(place_latency_ms=a.latency_ms, cancel_latency_ms=a.latency_ms)
    cfg["news"]["skip"] = False
    groups = {c: v["group"] for c, v in C.coins(cfg).items()}
    want = [c.strip().upper() for c in a.coins.split(",") if c.strip()] or list(groups)
    store, clock = Store(), Clock()
    eng = Engine(cfg, store, now=clock)
    n = 0
    for coin in want:
        rows, close = defaultdict(list), {}
        for f in sorted(data.glob(f"book_{coin}_*.csv")):
            with f.open(encoding="utf-8", errors="replace") as fh:
                for r in csv.DictReader(fh):
                    tk = r.get("ticker")
                    if tk not in res:
                        continue
                    try:
                        s = float(r["secs"])
                        if s < 100:
                            continue
                        rows[tk].append((s, float(r["yes_bid"]), float(r["yes_ask"]), float(r["bid_size"] or 0),
                                         float(r["ask_size"] or 0)))
                        close[tk] = float(r["close"])
                    except (ValueError, KeyError):
                        continue
        if not rows:
            continue
        trades = defaultdict(list)
        for f in trade_files:
            with f.open(encoding="utf-8", errors="replace") as fh:
                for r in csv.DictReader(fh):
                    tk = r.get("ticker")
                    if tk not in rows:
                        continue
                    try:
                        trades[tk].append((float(r["ts"]) - (close[tk] - 900), float(r["yes_price"]), r["taker_side"],
                                           float(r["count"] or 0)))
                    except (ValueError, KeyError):
                        continue
        # a ticker that appears in two files (old 6-minute file and the whole-window file): keep the larger set
        for tk, rr in rows.items():
            if len(rr) < 60 or tk not in trades:
                continue
            rr.sort()
            tr = sorted(set(trades[tk])) if len(trade_files) > 1 else sorted(trades[tk])
            m = Market(tk, coin, groups.get(coin, "crypto"), close[tk] - 900, close[tk], None)
            replay_window(eng, clock, m, rr, tr, "yes" if res[tk] == "UP" else "no", book_shift=a.shift)
            n += 1
        print(f"  {coin}: {len(rows)} windows replayed", flush=True)
    print(f"\n{n} windows. Cents per contract, standard error clustered by 15-minute slot:")
    for r in cfg["rules"]:
        s = store.scoreboard(r["id"], "paper")
        if not s.get("n"):
            print(f"  {r['id']:18s} no trades")
            continue
        rng = f"{s['lo95_c']:+.2f} to {s['hi95_c']:+.2f}" if s.get("se_c") else "-"
        print(f"  {r['id']:18s} {s['n']:5d} trades  mean {s['mean_c']:+.2f}c  range {rng}  won {s['win_rate']:.2%}  "
              f"bad {s['bad']}  avg entry {s['avg_px']:.2f}c")
    return 0


if __name__ == "__main__":
    sys.exit(main())
