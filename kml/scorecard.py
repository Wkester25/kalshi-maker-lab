"""The daily scorecard: one markdown report per day, written to reports/ and to reports/latest.md.

This is the input to the daily review (docs/REVIEW_LOOP.md). It answers, per rule: how many trades, what they
made per contract and how sure that is, whether filled bids did worse than unfilled ones, and what size the
evidence supports.

  python -m kml.scorecard          write today's report from data/lab.db
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from . import config as C
from .engine import ET
from .store import Store, ladder, stats


def _c(x, nd=2):
    return "-" if x is None else f"{x:+.{nd}f}c"


def build(store: Store, cfg: dict) -> str:
    now = time.time()
    since = C.score_since(cfg)
    L = [f"# Scorecard {datetime.fromtimestamp(now, ET):%Y-%m-%d %H:%M} ET", "",
         "Cents per contract. Ranges are 95%, with the standard error clustered by 15-minute slot. "
         "A rule earns real money only when its range stays above zero on data that did not choose its settings.", ""]
    for mode in ("paper", "live"):
        rows = []
        for r in cfg["rules"]:
            s = store.scoreboard(r["id"], mode, since)
            if not s.get("n"):
                continue
            rng = f"{s['lo95_c']:+.2f} to {s['hi95_c']:+.2f}" if s.get("se_c") else "-"
            lad = ladder(s)
            rows.append(f"| {r['id']} | {s['n']} | {_c(s['mean_c'])} | {rng} | {s['win_rate']:.2%} | {s['bad']} | "
                        f"{s['stopped']} | {s['avg_px']:.2f}c | {s['total_c'] / 100:+.2f} | "
                        f"{lad['contracts']} ({lad['why']}) |")
        if rows:
            L += [f"## {mode.capitalize()} results since {datetime.fromtimestamp(since, ET):%Y-%m-%d %H:%M} ET", "",
                  "| Rule | Trades | Mean | 95% range | Won | Lost or stopped | Stopped | Avg entry | Total $ | "
                  "Size the evidence supports |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"] + rows + [""]
    L += ["## By day (paper)", ""]
    for r in cfg["rules"]:
        tr = store.closed(r["id"], "paper", since)
        if not tr:
            continue
        days: dict[str, list] = {}
        for t in tr:
            days.setdefault(datetime.fromtimestamp(t["close_ts"], ET).strftime("%m-%d"), []).append(t)
        L.append(f"- **{r['id']}**: " + ", ".join(
            f"{d} {_c(stats(v)['mean_c'])} ({len(v)}, {stats(v)['bad']} bad)" for d, v in sorted(days.items())[-10:]))
    L += ["", "## Filled against unfilled bids", "",
          "If bids that never filled went on to win clearly more often than the trades did, fills are being "
          "selected against the rule.", ""]
    for r in cfg["rules"]:
        for mode in ("paper", "live"):
            s = store.scoreboard(r["id"], mode, since)
            u = store.unfilled(r["id"], mode, since)
            if not s.get("n") or not u:
                continue
            n = sum(x["n"] for x in u)
            won = sum(x["n"] * (x["won"] or 0) for x in u) / max(n, 1)
            L.append(f"- **{r['id']}** ({mode}): filled {s['n']} won {s['win_rate']:.2%}; unfilled {n} would have won "
                     f"{won:.2%}")
    ev = [e for e in store.events(400) if e["level"] in ("error", "halt", "warn")][:15]
    if ev:
        L += ["", "## Problems logged", ""] + [
            f"- {datetime.fromtimestamp(e['t'], ET):%m-%d %H:%M:%S} {e['level']}: {e['msg']}" for e in ev]
    return "\n".join(L) + "\n"


def write(store: Store, cfg: dict, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    text = build(store, cfg)
    p = out / f"{datetime.fromtimestamp(time.time(), ET):%Y-%m-%d}.md"
    p.write_text(text, encoding="utf-8")
    (out / "latest.md").write_text(text, encoding="utf-8")
    return p


if __name__ == "__main__":
    from . import config as C
    root = Path(__file__).resolve().parent.parent
    print(write(Store(root / "data" / "lab.db"), C.load(), root / "reports"))
