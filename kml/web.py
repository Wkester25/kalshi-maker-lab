"""The web page: status, scoreboard, settings. One token protects everything.

Bound to 127.0.0.1 by default. On a server, reach it through an SSH tunnel (see README) rather than opening
the port; this page can change trading settings.
"""
from __future__ import annotations

import hmac
import json
import time
from pathlib import Path

from aiohttp import web

from . import config as C
from . import scorecard
from .store import ladder

STATIC = Path(__file__).parent / "static"
ROOT = Path(__file__).resolve().parent.parent
EDITABLE_RULE = {"enabled", "live", "lo", "hi", "windows", "stop_c", "stop_ms", "contracts", "pricing", "coins",
                 "confirm_ms", "name", "note", "kind", "group"}
EDITABLE = {"paper": {"place_latency_ms", "cancel_latency_ms", "stop_slip_c"},
            "live": {"bankroll_dollars", "tail_fraction", "max_day_loss_dollars", "kill_switch_dollars_per_contract",
                     "max_order_dollars"},
            "news": {"skip", "weekdays_only", "times_et", "extra"}}


def make_app(app_obj) -> web.Application:
    cfg, eng, store = app_obj.cfg, app_obj.engine, app_obj.store
    token = cfg["web"]["token"]
    cache = {"t": 0.0, "v": None}

    def authed(req: web.Request) -> bool:
        got = req.headers.get("X-Token") or req.cookies.get("kml_token") or ""
        return hmac.compare_digest(got, token)

    @web.middleware
    async def auth(req: web.Request, handler):
        if req.path.startswith("/api/") and not authed(req):
            return web.json_response({"error": "token required"}, status=401)
        return await handler(req)

    async def index(req: web.Request):
        resp = web.FileResponse(STATIC / "index.html")
        t = req.query.get("token")
        if t and hmac.compare_digest(t, token):
            resp = web.HTTPFound("/")
            resp.set_cookie("kml_token", t, max_age=90 * 86400, httponly=True, samesite="Strict")
        return resp

    def scoreboard() -> list:
        if time.time() - cache["t"] < 3 and cache["v"] is not None:
            return cache["v"]
        day = eng.day_start()
        out = []
        for r in cfg["rules"]:
            row = {"id": r["id"], "name": r.get("name", r["id"]), "enabled": bool(r.get("enabled")),
                   "live_flag": bool(r.get("live")), "kind": r["kind"], "group": r["group"], "note": r.get("note", ""),
                   "stats": {}, "today": {}, "ladder": {}}
            for mode in ("paper", "live"):
                s = store.scoreboard(r["id"], mode)
                row["stats"][mode] = s
                row["today"][mode] = store.scoreboard(r["id"], mode, since=day)
                if s.get("n"):
                    row["ladder"][mode] = ladder(s)
            out.append(row)
        cache.update(t=time.time(), v=out)
        return out

    async def status(req):
        s = eng.status()
        s["feed"] = app_obj.feed_status()
        s["scoreboard"] = scoreboard()
        s["live"] = {"allowed_by_flag": app_obj.allow_live, "enabled": bool(cfg["live"].get("enabled")),
                     "halt": eng.live_halt, "has_keys": app_obj.client.has_keys}
        s["open_trades"] = store.open_trades()
        return web.json_response(s)

    async def get_config(req):
        c = json.loads(json.dumps(cfg))
        c["web"] = {"host": cfg["web"]["host"], "port": cfg["web"]["port"]}
        return web.json_response(c)

    def persist():
        over = C.read_overrides()
        over["rules"] = cfg["rules"]
        for sec in EDITABLE:
            over[sec] = cfg[sec]
        over.setdefault("live", {})["enabled"] = bool(cfg["live"].get("enabled"))
        over.setdefault("web", {})["token"] = token
        C.save_overrides(over)
        cache["v"] = None

    async def post_rule(req):
        body = await req.json()
        rid = body.get("id")
        cur = next((r for r in cfg["rules"] if r["id"] == rid), None)
        new = {**C.RULE_DEFAULTS, **(cur or {}), **{k: v for k, v in body.items() if k in EDITABLE_RULE}, "id": rid}
        if not rid:
            return web.json_response({"error": "rule id required"}, status=400)
        why = C.check_rule(new)
        if why:
            return web.json_response({"error": why}, status=400)
        if new.get("live") and new["kind"] != "maker":
            return web.json_response({"error": "only maker rules can trade live in this version"}, status=400)
        if cur:
            cur.clear()
            cur.update(new)                        # same object, so running windows see enable/disable at once
        else:
            cfg["rules"].append(new)
        persist()
        store.event("info", f"rule {rid} changed from the web page: " + json.dumps(
            {k: body[k] for k in body if k in EDITABLE_RULE})[:300])
        return web.json_response({"ok": True, "rule": new, "note": "band and time changes apply from the next window"})

    async def post_settings(req):
        body = await req.json()
        for sec, vals in body.items():
            if sec not in EDITABLE or not isinstance(vals, dict):
                continue
            for k, v in vals.items():
                if k in EDITABLE[sec]:
                    if isinstance(cfg[sec].get(k), (int, float)) and not isinstance(cfg[sec].get(k), bool):
                        try:
                            v = float(v)
                        except (TypeError, ValueError):
                            return web.json_response({"error": f"{sec}.{k} must be a number"}, status=400)
                        if v < 0:
                            return web.json_response({"error": f"{sec}.{k} can't be negative"}, status=400)
                    cfg[sec][k] = v
        persist()
        store.event("info", "settings changed from the web page: " + json.dumps(body)[:300])
        return web.json_response({"ok": True})

    async def post_live(req):
        body = await req.json()
        on = bool(body.get("enabled"))
        if on and not app_obj.allow_live:
            return web.json_response({"error": "live trading is locked: restart the program with --allow-live"},
                                     status=403)
        if on and not app_obj.client.has_keys:
            return web.json_response({"error": "no API keys loaded"}, status=400)
        cfg["live"]["enabled"] = on
        if on and body.get("clear_halt"):
            eng.live_halt = None
        persist()
        store.event("halt" if not on else "info", f"live trading switched {'ON' if on else 'OFF'} from the web page")
        return web.json_response({"ok": True, "note": "applies to windows that open from now on"})

    async def post_halt(req):
        eng.halt_live("stopped from the web page")
        cfg["live"]["enabled"] = False
        persist()
        note = await app_obj.live.cancel_everything() if app_obj.live and app_obj.allow_live else "no live orders"
        return web.json_response({"ok": True, "note": note})

    async def trades(req):
        return web.json_response(store.trades(req.query.get("rule") or None, int(req.query.get("limit", 200))))

    async def events(req):
        return web.json_response(store.events(int(req.query.get("limit", 200))))

    async def post_scorecard(req):
        p = scorecard.write(store, cfg, ROOT / "reports")
        return web.json_response({"ok": True, "path": str(p), "text": p.read_text(encoding="utf-8")})

    app = web.Application(middlewares=[auth])
    app.add_routes([web.get("/", index), web.get("/api/status", status), web.get("/api/config", get_config),
                    web.post("/api/rule", post_rule), web.post("/api/settings", post_settings),
                    web.post("/api/live", post_live), web.post("/api/halt", post_halt), web.get("/api/trades", trades),
                    web.get("/api/events", events), web.post("/api/scorecard", post_scorecard)])
    return app
