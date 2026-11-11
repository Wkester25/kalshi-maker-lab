"""Run with:  python -m unittest discover -s tests -v      (no network, no keys needed)"""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kml import config as C                      # noqa: E402
from kml.book import Book                         # noqa: E402
from kml.engine import Engine, Market, taker_fee_c  # noqa: E402
from kml.replay import Clock                      # noqa: E402
from kml.store import Store, ladder, stats        # noqa: E402

SNAP = {"market_ticker": "T", "yes_dollars_fp": [["0.0800", "300.00"], ["0.2200", "333.00"]],
        "no_dollars_fp": [["0.5400", "20.00"], ["0.5600", "146.00"]]}


def cfg_with(**rule):
    cfg = copy.deepcopy(C.DEFAULTS)
    r = {**C.RULE_DEFAULTS, "id": "t", "group": "crypto", "coins": ["BTC"], "kind": "maker", "lo": 97.0, "hi": 99.5,
         "windows": [[540, 720]], "confirm_ms": 0, **rule}
    assert C.check_rule(r) is None, C.check_rule(r)
    cfg["rules"] = [r]
    cfg["news"]["skip"] = False
    cfg["paper"].update(place_latency_ms=100, cancel_latency_ms=100)
    return cfg


def setup(**rule):
    clock, store = Clock(), Store()
    eng = Engine(cfg_with(**rule), store, now=clock)
    m = Market("KXBTC15M-X", "BTC", "crypto", 1000.0, 1900.0, 70000.0)
    clock.t = 1000.0 + 600
    eng.add_market(m)
    return eng, clock, store, m


class TestBook(unittest.TestCase):
    def test_snapshot_no_leg_pricing(self):
        b = Book(use_yes_price=False)
        b.snapshot(SNAP, 1.0)
        self.assertEqual(b.top(), (220, 333.0, 440, 146.0))          # NO bid 56c = YES ask 44c
        self.assertEqual(b.quote("NO"), (560, 146.0, 780, 333.0))

    def test_snapshot_yes_leg_pricing(self):
        b = Book(use_yes_price=True)
        b.snapshot({"yes_dollars_fp": [["0.9700", "10.00"]], "no_dollars_fp": [["0.9800", "5.00"], ["0.9900", "7"]]}, 1.0)
        self.assertEqual(b.top(), (970, 10.0, 980, 5.0))
        self.assertEqual(b.favourite(), "YES")

    def test_delta_and_removal(self):
        b = Book(use_yes_price=False)
        b.snapshot(SNAP, 1.0)
        b.delta({"price_dollars": "0.2200", "delta_fp": "-333.00", "side": "yes", "ts_ms": 5}, 2.0)
        self.assertEqual(b.top()[0], 80)
        b.delta({"price_dollars": "0.5700", "delta_fp": "12.00", "side": "no"}, 3.0)
        self.assertEqual(b.top()[2:], (430, 12.0))
        self.assertEqual(b.exch_ms, 5)

    def test_own_orders_are_removed(self):
        b = Book()
        b.set_top(97.5, 98.0, 100, 50, 1.0)
        b.yes[976] = 1.0                                              # our own improving bid, alone at its level
        b._changed(1.0, {})
        self.assertEqual(b.quote("YES")[0], 976)                      # the public book shows us as best bid
        self.assertEqual(b.quote("YES", {("YES", 976): 1.0})[:2], (975, 100))   # with us removed: the market's bid

    def test_crossed_book_is_counted(self):
        b = Book(use_yes_price=True)
        b.snapshot({"yes_dollars_fp": [["0.9700", "1"]], "no_dollars_fp": [["0.0200", "1"]]}, 1.0)
        self.assertEqual(b.crossed, 1)


class TestEngine(unittest.TestCase):
    def test_improves_then_fills_on_trade_through(self):
        eng, clock, store, m = setup()
        eng.on_top(m.ticker, 97.5, 98.0, 500, 400)
        eng.tick()
        st = eng.states[m.ticker][0]
        self.assertEqual((st.order.side, st.order.px, st.order.queue), ("YES", 976, 0.0))
        eng.on_trade(m.ticker, 975, 10, "no", clock.t + 0.05)          # before the order is live: no fill
        self.assertIsNone(st.pos)
        eng.on_trade(m.ticker, 975, 10, "no", clock.t + 0.2)           # through our 97.6 bid
        self.assertEqual((st.pos.px, st.pos.n), (976, 1.0))
        eng.settle(m.ticker, "yes")
        t = store.trades()[0]
        self.assertEqual((t["exit"], round(t["pnl_c"], 2)), ("won", 2.4))

    def test_never_raises_against_itself(self):
        eng, clock, store, m = setup()
        eng.on_top(m.ticker, 97.5, 98.5, 500, 400)
        eng.tick()
        st = eng.states[m.ticker][0]
        px = st.order.px
        for _ in range(10):
            clock.t += 0.2
            eng.tick()
        self.assertEqual(st.order.px, px)                              # market unchanged: our bid stays put

    def test_join_waits_for_the_queue(self):
        eng, clock, store, m = setup(pricing="join")
        eng.on_top(m.ticker, 98.0, 98.1, 300, 400)
        eng.tick()
        st = eng.states[m.ticker][0]
        self.assertEqual((st.order.px, st.order.queue), (980, 300))
        eng.on_trade(m.ticker, 980, 250, "no", clock.t + 1)
        self.assertIsNone(st.pos)
        eng.on_trade(m.ticker, 980, 60, "no", clock.t + 2)
        self.assertIsNotNone(st.pos)

    def test_wrong_side_trades_do_not_fill(self):
        eng, clock, store, m = setup()
        eng.on_top(m.ticker, 97.5, 98.0, 500, 400)
        eng.tick()
        eng.on_trade(m.ticker, 500, 10, "yes", clock.t + 1)            # taker bought YES: makers bought NO
        self.assertIsNone(eng.states[m.ticker][0].pos)

    def test_cancel_has_latency_and_unfilled_order_is_logged(self):
        eng, clock, store, m = setup()
        eng.on_top(m.ticker, 97.5, 98.0, 500, 400)
        eng.tick()
        st = eng.states[m.ticker][0]
        clock.t += 1
        eng.on_top(m.ticker, 95.0, 96.0, 500, 400)                     # market leaves the band
        eng.tick()
        self.assertIsNotNone(st.order.cancel_at)
        eng.on_trade(m.ticker, 960, 5, "no", clock.t + 0.05)           # hit before the cancel lands
        self.assertIsNotNone(st.pos)
        eng2, clock2, store2, m2 = setup()
        eng2.on_top(m2.ticker, 97.5, 98.0, 500, 400)
        eng2.tick()
        eng2.on_top(m2.ticker, 95.0, 96.0, 500, 400)
        eng2.tick()
        clock2.t += 0.5
        eng2.tick()
        self.assertIsNone(eng2.states[m2.ticker][0].order)
        eng2.settle(m2.ticker, "no")
        o = store2._q("SELECT * FROM orders")[0]
        self.assertEqual((o["outcome"], o["won"]), ("cancelled", 0))

    def test_stop_needs_two_seconds_below(self):
        eng, clock, store, m = setup(stop_c=50.0)
        eng.on_top(m.ticker, 97.5, 98.0, 500, 400)
        eng.tick()
        eng.on_trade(m.ticker, 970, 5, "no", clock.t + 1)
        st = eng.states[m.ticker][0]
        clock.t += 2
        eng.on_top(m.ticker, 45.0, 47.0, 500, 400)
        eng.tick()
        self.assertIsNotNone(st.pos)                                   # first low reading: wait
        clock.t += 1
        eng.on_top(m.ticker, 60.0, 62.0, 500, 400)
        eng.tick()                                                     # recovered: counter resets
        clock.t += 1
        eng.on_top(m.ticker, 44.0, 46.0, 500, 400)
        eng.tick()
        clock.t += 2.1
        eng.tick()
        self.assertIsNone(st.pos)
        t = store.trades()[0]
        self.assertEqual(t["exit"], "stopped")
        self.assertAlmostEqual(t["pnl_c"], 43.5 - 97.6 - taker_fee_c(1, 435), places=4)

    def test_taker_pays_the_fee_and_the_ask(self):
        eng, clock, store, m = setup(kind="taker", lo=98.0, hi=99.5, contracts=10, windows=[[540, 720]])
        eng.on_top(m.ticker, 97.0, 98.0, 500, 400)
        eng.tick()
        st = eng.states[m.ticker][0]
        self.assertEqual((st.pos.px, st.pos.n, st.pos.fee_c), (980, 10, 2))
        eng.settle(m.ticker, "yes")
        self.assertAlmostEqual(store.trades()[0]["pnl_c"], 10 * 2.0 - 2)

    def test_outside_window_and_news(self):
        eng, clock, store, m = setup(windows=[[700, 720]])
        eng.on_top(m.ticker, 97.5, 98.0, 500, 400)
        eng.tick()
        self.assertIsNone(eng.states[m.ticker][0].order)
        cfg = cfg_with()
        cfg["news"].update(skip=True, weekdays_only=False, times_et=["00:00"])
        clock2 = Clock()
        eng2 = Engine(cfg, Store(), now=clock2)
        import datetime
        from kml.engine import ET
        o = datetime.datetime(2026, 11, 11, 23, 50, tzinfo=ET).timestamp()
        m2 = Market("KXBTC15M-N", "BTC", "crypto", o, o + 900, None)
        clock2.t = o + 600
        eng2.add_market(m2)
        self.assertIn("00:00", m2.news)
        eng2.on_top(m2.ticker, 97.5, 98.0, 500, 400)
        eng2.tick()
        self.assertIsNone(eng2.states[m2.ticker][0].order)

    def test_no_bid_when_no_side_is_the_favourite(self):
        eng, clock, store, m = setup()
        eng.on_top(m.ticker, 2.0, 2.4, 400, 500)                       # NO is the favourite: bid 97.6, ask 98.0
        eng.tick()
        st = eng.states[m.ticker][0]
        self.assertEqual((st.order.side, st.order.px), ("NO", 977))
        eng.on_trade(m.ticker, 25, 3, "yes", clock.t + 1)              # taker bought YES at 2.5c = sold NO at 97.5c
        self.assertEqual(st.pos.side, "NO")
        eng.settle(m.ticker, "yes")
        self.assertAlmostEqual(store.trades()[0]["pnl_c"], -97.7)

    def test_live_is_off_by_default(self):
        eng, clock, store, m = setup(live=True)
        self.assertEqual(eng.states[m.ticker][0].mode, "paper")


class TestStats(unittest.TestCase):
    def test_cluster_stats_and_ladder(self):
        rows = [{"close_ts": 900.0 * (i // 3), "pnl_c": 2.0, "n": 1, "px": 98.0, "exit": "won", "coin": "BTC",
                 "t_fill": i} for i in range(1200)]
        rows[5]["pnl_c"] = -98.0
        s = stats(rows)
        self.assertEqual((s["n"], s["bad"], s["slots"]), (1200, 1, 400))
        self.assertGreater(s["t"], 2)
        self.assertEqual(ladder(s)["contracts"], 2)
        self.assertEqual(ladder(stats(rows[:500]))["contracts"], 1)


class TestFeedAndWeb(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["KML_CONFIG"] = str(Path(self.tmp.name) / "config.json")
        os.environ["KML_KEYS_FILE"] = str(Path(self.tmp.name) / "keys.json")
        import importlib
        import kml.app as A
        import kml.config as CC
        import kml.kalshi_rest as KR
        importlib.reload(KR)
        importlib.reload(CC)
        importlib.reload(A)
        A.ROOT = Path(self.tmp.name)
        self.cfg = CC.load()
        self.cfg["source"] = "rest"
        self.app = A.App(self.cfg)
        e = self.app.engine
        self.m = Market("KXBTC15M-W", "BTC", "crypto", e.now() - 650, e.now() + 250, 70000.0, True)
        e.add_market(self.m)

    async def asyncTearDown(self):
        self.app.rec.close()
        self.app.store.db.close()
        self.tmp.cleanup()

    async def test_websocket_messages_from_the_docs(self):
        a, tk = self.app, self.m.ticker
        self.assertTrue(a.on_ws("orderbook_snapshot", {"market_ticker": tk, "yes_dollars_fp": [["0.9900", "50.00"]],
                                                       "no_dollars_fp": [["0.9930", "20.00"]]}, 1.0, None))
        self.assertEqual(self.m.book.top(), (990, 50.0, 993, 20.0))
        a.on_ws("orderbook_delta", {"market_ticker": tk, "price_dollars": "0.9910", "delta_fp": "5.00", "side": "yes",
                                    "ts_ms": 1669149841000}, 1.1, 1669149841123)
        self.assertEqual(self.m.book.top()[0], 991)
        a.on_ws("trade", {"market_ticker": tk, "yes_price_dollars": "0.9900", "no_price_dollars": "0.0100",
                          "count_fp": "136.00", "taker_side": "no", "ts_ms": int(a.engine.now() * 1000)}, 1.2, None)
        self.assertEqual(self.m.trades, 1)
        a.on_ws("cfbenchmarks_value", {"index_id": "BRTI", "received_at": 1, "data": json.dumps(
            {"type": "value", "id": "BRTI", "time": 1710000000123, "value": "68000.12"}),
            "avg_60s_data": {"value": "68000.12000000", "window_size": 3},
            "last_60s_windowed_average_15min": {"value": "68000.23000000", "window_size": 14}}, 1.3, None)
        self.assertEqual(a.engine.index["BRTI"]["value"], 68000.12)
        self.assertEqual((a.engine.index["BRTI"]["win_avg"], a.engine.index["BRTI"]["win_n"]), (68000.23, 14))
        a.on_ws("pyth_value", {"underlying_ticker": "Metal.XAU/USD", "value_usd": "4100.50000000",
                               "source_ts_ms": 5}, 1.4, None)
        self.assertEqual(a.engine.pyth["Metal.XAU/USD"]["value"], 4100.5)
        self.assertFalse(a.on_ws("something_new", {}, 1.5, None))
        self.assertGreater(a.rec.rows, 3)

    async def test_web_api(self):
        from aiohttp.test_utils import TestClient, TestServer
        from kml.web import make_app
        tok = self.cfg["web"]["token"]
        async with TestClient(TestServer(make_app(self.app))) as c:
            self.assertEqual((await c.get("/api/status")).status, 401)
            hd = {"X-Token": tok}
            s = await (await c.get("/api/status", headers=hd)).json()
            self.assertEqual(s["markets"][0]["coin"], "BTC")
            self.assertFalse(s["live"]["allowed_by_flag"])
            r = await c.post("/api/live", json={"enabled": True}, headers=hd)
            self.assertEqual(r.status, 403)                                  # locked without --allow-live
            r = await c.post("/api/rule", json={"id": "crypto_99", "lo": 99.9, "hi": 99.0}, headers=hd)
            self.assertEqual(r.status, 400)
            r = await c.post("/api/rule", json={"id": "commodity_take98", "live": True}, headers=hd)
            self.assertEqual(r.status, 400)                                  # taker rules are paper only
            r = await c.post("/api/rule", json={"id": "crypto_99", "enabled": False}, headers=hd)
            self.assertEqual(r.status, 200)
            self.assertFalse(next(x for x in self.cfg["rules"] if x["id"] == "crypto_99")["enabled"])
            cfg = await (await c.get("/api/config", headers=hd)).json()
            self.assertNotIn("token", cfg["web"])
            self.assertEqual((await c.post("/api/scorecard", json={}, headers=hd)).status, 200)
            self.assertEqual((await c.get("/", headers=hd)).status, 200)

    async def test_login_page(self):
        from aiohttp.test_utils import TestClient, TestServer
        import kml.config as CC
        from kml.web import make_app
        self.cfg["web"]["password_hash"] = CC.hash_password("correct horse")
        async with TestClient(TestServer(make_app(self.app))) as c:
            r = await c.get("/", allow_redirects=False)
            self.assertEqual((r.status, r.headers["Location"]), (302, "/login"))      # not signed in: sent to login
            self.assertEqual((await c.get("/login")).status, 200)
            self.assertEqual((await c.get("/api/status")).status, 401)
            r = await c.post("/api/login", json={"username": "admin", "password": "wrong"})
            self.assertEqual(r.status, 401)
            r = await c.post("/api/login", json={"username": "nobody", "password": "correct horse"})
            self.assertEqual(r.status, 401)
            r = await c.post("/api/login", json={"username": "admin", "password": "correct horse"})
            self.assertEqual(r.status, 200)
            self.assertIn("HttpOnly", r.headers["Set-Cookie"])
            self.assertEqual((await c.get("/api/status")).status, 200)                # the cookie now signs us in
            self.assertEqual((await c.get("/", allow_redirects=False)).status, 200)
            await c.post("/api/logout", json={})
            self.assertEqual((await c.get("/api/status")).status, 401)
            c.session.cookie_jar.update_cookies({"kml_session": "admin|9999999999|forged"})
            self.assertEqual((await c.get("/api/status")).status, 401)                # a forged cookie is refused
            c.session.cookie_jar.clear()
            for _ in range(5):
                await c.post("/api/login", json={"username": "admin", "password": "x"})
            r = await c.post("/api/login", json={"username": "admin", "password": "correct horse"})
            self.assertEqual(r.status, 429)                                           # locked out after 5 misses

    async def test_keys_api(self):
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
        except ImportError:
            self.skipTest("needs the cryptography package to make a test key")
        from aiohttp.test_utils import TestClient, TestServer
        import kml.config as CC
        import kml.kalshi_rest as KR
        from kml.web import make_app
        pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()).decode()
        pub = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        self.cfg["web"]["password_hash"] = CC.hash_password("pw-for-keys")
        self.cfg["source"] = "ws"                                   # wants the WebSocket, running on REST (no keys)
        kid = "a1b2c3d4-0000-4000-8000-123456789abc"
        async with TestClient(TestServer(make_app(self.app))) as c:
            hd = {"X-Token": self.cfg["web"]["token"]}
            self.assertEqual((await c.get("/keys", allow_redirects=False)).status, 302)     # needs sign-in
            self.assertEqual((await c.get("/keys", headers=hd)).status, 200)
            d = await (await c.get("/api/keys", headers=hd)).json()
            self.assertFalse(d["keys"]["real"]["present"])
            self.assertFalse(d["program"]["has_keys"])
            r = await c.post("/api/keys", json={"env": "real", "key_id": kid, "pem": pem, "password": "nope"}, headers=hd)
            self.assertEqual(r.status, 403)
            r = await c.post("/api/keys", json={"env": "real", "key_id": kid, "pem": pub, "password": "pw-for-keys"}, headers=hd)
            self.assertEqual(r.status, 400)
            self.assertIn("PUBLIC key", (await r.json())["error"])
            r = await c.post("/api/keys", json={"env": "real", "key_id": "x", "pem": pem, "password": "pw-for-keys"}, headers=hd)
            self.assertEqual(r.status, 400)
            r = await c.post("/api/keys", json={"env": "real", "key_id": kid, "pem": pem.replace("\n", "\r\n"),
                                                "password": "pw-for-keys"}, headers=hd)
            body = await r.json()
            self.assertEqual(r.status, 200, body)
            self.assertEqual(body["type"], "RSA 2048-bit")
            self.assertTrue(body["restart_needed"])                 # running on REST, wants the WebSocket
            self.assertTrue(self.app.client.has_keys)               # picked up without a restart
            self.assertIsNotNone(self.app.engine.live_exec)
            stored = KR.KEYS_FILE.parent / "secrets" / "real_private_key.pem"
            self.assertTrue(stored.exists())
            if os.name == "posix":
                self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
                self.assertEqual(KR.KEYS_FILE.stat().st_mode & 0o777, 0o600)
            text = await (await c.get("/api/keys", headers=hd)).text()
            self.assertNotIn("PRIVATE KEY", text)                   # the key itself never goes back to the browser
            self.assertNotIn(kid, text)                             # and the key ID is masked
            d = json.loads(text)["keys"]["real"]
            self.assertEqual((d["type"], d["stored_here"]), ("RSA 2048-bit", True))
            orig = KR.Client.balance
            KR.Client.balance = lambda self_: {"balance": 12345}
            try:
                t = await (await c.post("/api/keys/test", json={"env": "real"}, headers=hd)).json()
            finally:
                KR.Client.balance = orig
            self.assertEqual((t["ok"], t["balance"]), (True, 123.45))
            ext = Path(self.tmp.name) / "my_key.pem"
            ext.write_text(pem)
            r = await c.post("/api/keys", json={"env": "sandbox", "key_id": kid, "path": str(ext),
                                                "password": "pw-for-keys"}, headers=hd)
            self.assertEqual(r.status, 200)
            r = await c.post("/api/keys/delete", json={"env": "sandbox", "password": "pw-for-keys"}, headers=hd)
            self.assertEqual(r.status, 200)
            self.assertTrue(ext.exists())                           # a file the user pointed to is never deleted
            r = await c.post("/api/keys/delete", json={"env": "real", "password": "pw-for-keys"}, headers=hd)
            self.assertEqual(r.status, 200)
            self.assertFalse(stored.exists())
            self.assertFalse(self.app.client.has_keys)
            self.assertEqual((await c.post("/api/restart", json={}, headers=hd)).status, 400)   # not a service here

    def test_password_hash(self):
        import kml.config as CC
        h = CC.hash_password("s3cret-pass")
        self.assertTrue(h.startswith("pbkdf2$") and "s3cret" not in h)
        self.assertTrue(CC.check_password("s3cret-pass", h))
        self.assertFalse(CC.check_password("s3cret-pasS", h))
        self.assertFalse(CC.check_password("anything", ""))


if __name__ == "__main__":
    unittest.main()
