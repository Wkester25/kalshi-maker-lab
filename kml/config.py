"""Settings: defaults, the frozen rules, loading and saving config.json.

config.json (project folder, never committed) holds only what differs from the defaults below.
Rules are matched by "id": a rule in config.json overrides fields of the default rule with the same id,
and a rule with a new id is added.
"""
from __future__ import annotations

import copy
import json
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = Path(os.environ.get("KML_CONFIG", ROOT / "config.json"))

CRYPTO = {
    "BTC": {"series": "KXBTC15M", "index": "BRTI"},
    "ETH": {"series": "KXETH15M", "index": "ETHUSD_RTI"},
    "SOL": {"series": "KXSOL15M", "index": "SOLUSD_RTI"},
    "DOGE": {"series": "KXDOGE15M", "index": "DOGEUSD_RTI"},
    "HYPE": {"series": "KXHYPE15M", "index": "HYPEUSD_RTI"},
    "XRP": {"series": "KXXRP15M", "index": "XRPUSD_RTI"},
    "BNB": {"series": "KXBNB15M", "index": "BNBUSD_RTI"},
}
COMMODITY = {
    "GOLD": {"series": "KXGOLD15M", "pyth": "Metal.XAU/USD"},
    "SILVER": {"series": "KXSILVER15M", "pyth": "Metal.XAG/USD"},
    "PLATINUM": {"series": "KXPLATINUM15M", "pyth": "Metal.XPT/USD"},
    "PALLADIUM": {"series": "KXPALLADIUM15M", "pyth": "Metal.XPD/USD"},
    "COPPER": {"series": "KXCOPPER15M", "pyth": ""},
    "WTI": {"series": "KXWTI15M", "pyth": ""},
    "BRENT": {"series": "KXBRENT15M", "pyth": ""},
    "NATGAS": {"series": "KXNATGAS15M", "pyth": "Commodities.Index.NATGAS/USD"},
}

# The frozen rules. Numbers come from the Nov 2026 review; do not tune them on the data that scores them.
#   kind     maker = rest a bid and wait; taker = buy at the ask
#   lo, hi   band in cents: the favourite's best bid (maker) or best ask (taker) must be in [lo, hi)
#   windows  seconds since the 15-minute window opened, inclusive
#   stop_c   sell if our side's bid stays below this for stop_ms (0 = hold to settlement)
#   pricing  improve = one tick above the best bid, never crossing; join = at the best bid
RULES = [
    {"id": "crypto_99", "name": "Crypto 99c+ early and final 30 s", "group": "crypto",
     "coins": ["BTC", "ETH", "SOL", "DOGE", "HYPE"], "kind": "maker", "lo": 99.0, "hi": 99.9,
     "windows": [[600, 720], [870, 897]], "stop_c": 0, "pricing": "improve",
     "note": "Closest match to the Reddit trader's arithmetic. 569 simulated trades, 0 losses, on 2.5 days."},
    {"id": "commodity_mid", "name": "Commodity favourite 75-90c", "group": "commodity", "coins": "all",
     "kind": "maker", "lo": 75.0, "hi": 90.0, "windows": [[120, 720]], "stop_c": 0, "pricing": "improve",
     "note": "Tape shows +5.7c for makers on this side; a slow bot simulated +1.9c, range includes zero."},
    {"id": "commodity_take98", "name": "Commodity take favourite 98-99.5c", "group": "commodity", "coins": "all",
     "kind": "taker", "lo": 98.0, "hi": 99.5, "windows": [[240, 540]], "stop_c": 0, "contracts": 10,
     "note": "Taker, so it needs size: the fee rounds up to 1c on a single contract. Paper only for now."},
    {"id": "commodity_early", "name": "Commodity favourite 96-99.5c (Late Maker rule)", "group": "commodity",
     "coins": ["GOLD", "SILVER", "WTI", "BRENT", "NATGAS", "PLATINUM", "PALLADIUM"], "kind": "maker",
     "lo": 96.0, "hi": 99.5, "windows": [[540, 780]], "stop_c": 50.0, "pricing": "improve",
     "note": "The Late Maker bot's commodity leg. About +0.8c pooled, +0.2c on the newer windows."},
    {"id": "crypto_early", "name": "Crypto favourite 97-99.5c (Late Maker rule, control)", "group": "crypto",
     "coins": ["BTC", "ETH", "DOGE", "HYPE", "XRP", "BNB"], "kind": "maker", "lo": 97.0, "hi": 99.5,
     "windows": [[540, 720]], "stop_c": 50.0, "pricing": "improve",
     "note": "Kept as a control. No support in the review: -0.3c on the newer windows."},
]
RULE_DEFAULTS = {"enabled": True, "live": False, "contracts": 1, "stop_c": 0.0, "stop_ms": 2000,
                 "confirm_ms": 400, "pricing": "improve", "note": ""}

DEFAULTS = {
    "env": "real",
    # market data source: "ws" (WebSocket, needs API keys) or "rest" (public one-second polling, no keys)
    "source": "ws",
    "ws_url": "wss://api.elections.kalshi.com/trade-api/ws/v2",
    "ws_sign_path": "/trade-api/ws/v2",
    "use_yes_price": True,
    "markets": {"crypto": CRYPTO, "commodity": COMMODITY},
    "record": {"enabled": True, "mode": "top", "dir": "data/rec", "gzip": True,
               "index": True, "index_5hz": False, "pyth": ["all"]},
    "paper": {"place_latency_ms": 150, "cancel_latency_ms": 150, "stop_slip_c": 0.5},
    "live": {"enabled": False, "bankroll_dollars": 100.0, "tail_fraction": 0.10, "max_day_loss_dollars": 10.0,
             "kill_switch_dollars_per_contract": 6.0, "max_order_dollars": 20.0},
    "news": {"skip": True, "weekdays_only": True, "times_et": ["08:30", "10:00"],
             "extra": [{"weekday": 2, "time": "10:30", "group": "commodity", "why": "EIA petroleum"},
                       {"weekday": 3, "time": "10:30", "group": "commodity", "why": "EIA gas storage"}]},
    "eval_ms": 200,
    "web": {"host": "127.0.0.1", "port": 8787, "token": ""},
    "rules": RULES,
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if k == "rules":
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _rules(over: list | None) -> list:
    by_id = {r["id"]: {**RULE_DEFAULTS, **copy.deepcopy(r)} for r in RULES}
    for r in over or []:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        by_id[r["id"]] = {**RULE_DEFAULTS, **by_id.get(r["id"], {}), **copy.deepcopy(r)}
    return list(by_id.values())


def check_rule(r: dict) -> str | None:
    """A plain-English reason the rule can't be used, or None."""
    try:
        if r["kind"] not in ("maker", "taker"):
            return "kind must be maker or taker"
        if r["group"] not in ("crypto", "commodity"):
            return "group must be crypto or commodity"
        lo, hi = float(r["lo"]), float(r["hi"])
        if not (1 <= lo < hi <= 99.9):
            return "band must satisfy 1 <= lo < hi <= 99.9"
        if not r["windows"] or any(not (0 <= float(a) < float(b) <= 898) for a, b in r["windows"]):
            return "each window must be [start, end] in seconds with 0 <= start < end <= 898"
        if not (1 <= int(r["contracts"]) <= 1000):
            return "contracts must be between 1 and 1000"
        if float(r.get("stop_c", 0)) < 0 or float(r.get("stop_c", 0)) >= lo:
            return "stop_c must be 0 or below the band"
        if r.get("pricing", "improve") not in ("improve", "join"):
            return "pricing must be improve or join"
        if r["coins"] != "all" and not isinstance(r["coins"], list):
            return "coins must be a list or 'all'"
    except (KeyError, TypeError, ValueError) as e:
        return f"bad or missing field: {e}"
    return None


def load() -> dict:
    over = {}
    if CONFIG_FILE.exists():
        try:
            over = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise SystemExit(f"{CONFIG_FILE} is not valid JSON: {e}")
    cfg = _merge(DEFAULTS, over)
    cfg["rules"] = [r for r in _rules(over.get("rules")) if check_rule(r) is None]
    if not cfg["web"].get("token"):
        cfg["web"]["token"] = secrets.token_urlsafe(18)
        save_overrides({**over, "web": {**over.get("web", {}), "token": cfg["web"]["token"]}})
    return cfg


def save_overrides(over: dict) -> None:
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(over, indent=1), encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)


def read_overrides() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def coins(cfg: dict) -> dict[str, dict]:
    """coin -> {series, group, index?, pyth?}"""
    out = {}
    for grp, d in cfg["markets"].items():
        for c, v in d.items():
            out[c] = {**v, "group": grp}
    return out


def rule_coins(rule: dict, cfg: dict) -> list[str]:
    allc = [c for c, v in coins(cfg).items() if v["group"] == rule["group"]]
    return allc if rule["coins"] == "all" else [c for c in rule["coins"] if c in allc]
