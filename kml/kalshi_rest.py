"""
Kalshi REST access: request signing, market data, resting buy orders, IOC sells, cancels.
Carried over from the Late Maker bot's client, which has placed real orders with this exact code.
Standard library only (uses the 'cryptography' package for signing if it is installed).

Keys live in keys.json in the project folder (never committed), same shape as the old lm_keys.json:
  {"real": {"key_id": "...", "key_file": "/path/to/private_key.pem"},
   "sandbox": {"key_id": "...", "key_file": "..."}}
or in the environment: KALSHI_KEY_ID and KALSHI_KEY_FILE.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEYS_FILE = Path(os.environ.get("KML_KEYS_FILE", ROOT / "keys.json"))
BASES = {"real": ["https://api.elections.kalshi.com/trade-api/v2", "https://external-api.kalshi.com/trade-api/v2"],
         "sandbox": ["https://demo-api.kalshi.co/trade-api/v2", "https://external-api.demo.kalshi.co/trade-api/v2"]}
PUBLIC = BASES["real"][0]          # market data always comes from the live exchange


class KalshiError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


def iso_ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def num(d: dict, name: str, default=None):
    """A count or money field in contracts or cents, whichever form Kalshi sent."""
    if not isinstance(d, dict):
        return default
    if d.get(name + "_dollars") not in (None, ""):
        return float(d[name + "_dollars"]) * 100
    if d.get(name + "_fp") not in (None, ""):
        return float(d[name + "_fp"])
    if d.get(name) not in (None, ""):
        return float(d[name])
    return default


# ------------------------------------------------------------------ signing
def _der(b: bytes, i: int):
    tag, ln = b[i], b[i + 1]
    i += 2
    if ln & 0x80:
        k = ln & 0x7F
        ln, i = int.from_bytes(b[i:i + k], "big"), i + k
    return tag, b[i:i + ln], i + ln


def _seq(b: bytes):
    out, i = [], 0
    while i < len(b):
        t, v, i = _der(b, i)
        out.append((t, v))
    return out


RSA_OID = bytes.fromhex("2a864886f70d010101")
ED25519_OID = bytes.fromhex("2b6570")


def _pem_body(pem: str) -> tuple[str, bytes]:
    """(header line, DER bytes) with a plain-English error for files that aren't a private key."""
    text = pem.lstrip("\ufeff")
    lines = [x.strip() for x in text.strip().splitlines()]
    head = next((x for x in lines if x.startswith("-----BEGIN")), "")
    if not head:
        raise ValueError("that file isn't a PEM key (no '-----BEGIN ... PRIVATE KEY-----' line). Pick the private "
                         "key file Kalshi gave you when you created the API key")
    if "PUBLIC KEY" in head:
        raise ValueError("that's the PUBLIC key. Pick the PRIVATE key file Kalshi gave you when you created the key")
    if "CERTIFICATE" in head:
        raise ValueError("that's a certificate, not a private key")
    if "ENCRYPTED" in head or any(x.startswith("Proc-Type: 4,ENCRYPTED") for x in lines):
        raise ValueError("the key is password-protected; this bot needs an unprotected key (or: pip install cryptography)")
    body = "".join(x for x in lines if x and not x.startswith("-----") and ":" not in x)
    return head, base64.b64decode(body)


def private_key(pem: str):
    """An RSA or Ed25519 signer from a PEM private key, without extra packages."""
    head, der = _pem_body(pem)
    items = _seq(_der(der, 0)[1])
    if len(items) >= 3 and items[1][0] == 0x30:                       # PKCS#8 wrapper: version, algorithm, key
        oid = _seq(items[1][1])[0][1]
        inner = items[2][1]
        if oid == ED25519_OID:
            t, seed, _ = _der(inner, 0)                               # OCTET STRING holding the 32-byte seed
            return _PureEd25519(seed if t == 0x04 else inner)
        if oid != RSA_OID:
            raise ValueError(f"unsupported key type (OID {oid.hex()}); create an RSA key on Kalshi")
        items = _seq(_der(inner, 0)[1])
    return _PureRSA(items)


class _PureEd25519:
    """Ed25519 signing (RFC 8032) in plain Python."""
    P = 2 ** 255 - 19
    L = 2 ** 252 + 27742317777372353535851937790883648493
    D = -121665 * pow(121666, P - 2, P) % P

    def __init__(self, seed: bytes):
        if len(seed) != 32:
            raise ValueError("Ed25519 key has the wrong length")
        h = hashlib.sha512(seed).digest()
        a = int.from_bytes(h[:32], "little")
        a &= (1 << 254) - 8
        a |= 1 << 254
        self.a, self.prefix = a, h[32:]
        self.A = self._enc(self._mul(a, self._base()))

    def _add(self, p1, p2):
        P, D = self.P, self.D
        x1, y1, z1, t1 = p1
        x2, y2, z2, t2 = p2
        a = (y1 - x1) * (y2 - x2) % P
        b = (y1 + x1) * (y2 + x2) % P
        c = 2 * t1 * t2 * D % P
        d = 2 * z1 * z2 % P
        e, f, g, h = b - a, d - c, d + c, b + a
        return (e * f % P, g * h % P, f * g % P, e * h % P)

    def _mul(self, s, pt):
        q = (0, 1, 1, 0)
        while s:
            if s & 1:
                q = self._add(q, pt)
            pt = self._add(pt, pt)
            s >>= 1
        return q

    def _base(self):
        P = self.P
        y = 4 * pow(5, P - 2, P) % P
        x2 = (y * y - 1) * pow(self.D * y * y + 1, P - 2, P) % P
        x = pow(x2, (P + 3) // 8, P)
        if (x * x - x2) % P:
            x = x * pow(2, (P - 1) // 4, P) % P
        if x & 1:
            x = P - x
        return (x, y, 1, x * y % P)

    def _enc(self, pt):
        P = self.P
        x, y, z, _ = pt
        zi = pow(z, P - 2, P)
        x, y = x * zi % P, y * zi % P
        return int.to_bytes(y | ((x & 1) << 255), 32, "little")

    def sign(self, msg: bytes) -> bytes:
        r = int.from_bytes(hashlib.sha512(self.prefix + msg).digest(), "little") % self.L
        R = self._enc(self._mul(r, self._base()))
        k = int.from_bytes(hashlib.sha512(R + self.A + msg).digest(), "little") % self.L
        return R + int.to_bytes((r + k * self.a) % self.L, 32, "little")


class _PureRSA:
    """RSA-PSS SHA-256 signing without extra packages (fallback)."""

    def __init__(self, items):
        ints = [int.from_bytes(v, "big") for t, v in items if t == 0x02]
        if len(ints) < 9:
            raise ValueError("couldn't read that file as an RSA private key - pick the private key file Kalshi "
                             "gave you (or run: pip install cryptography)")
        _, self.n, self.e, self.d, self.p, self.q, self.dp, self.dq, self.qi = ints[:9]
        self.bits = self.n.bit_length()

    def sign(self, msg: bytes) -> bytes:
        h, hl = hashlib.sha256, 32
        em_bits = self.bits - 1
        em_len = (em_bits + 7) // 8
        salt = os.urandom(hl)
        hh = h(b"\0" * 8 + h(msg).digest() + salt).digest()
        db = b"\0" * (em_len - 2 * hl - 2) + b"\x01" + salt
        mask, c = b"", 0
        while len(mask) < len(db):
            mask += h(hh + c.to_bytes(4, "big")).digest()
            c += 1
        masked = bytearray(a ^ b for a, b in zip(db, mask))
        masked[0] &= 0xFF >> (8 * em_len - em_bits)
        m = int.from_bytes(bytes(masked) + hh + b"\xbc", "big")
        m1, m2 = pow(m, self.dp, self.p), pow(m, self.dq, self.q)
        s = m2 + self.q * ((self.qi * (m1 - m2)) % self.p)
        return s.to_bytes((self.bits + 7) // 8, "big")


class Signer:
    def __init__(self, pem: str):
        _pem_body(pem)                                               # clear errors for the wrong kind of file
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError:
            self._sign = private_key(pem).sign
            return
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        if isinstance(key, Ed25519PrivateKey):
            self._sign = key.sign
        else:
            pss = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH)
            self._sign = lambda m: key.sign(m, pss, hashes.SHA256())

    def headers(self, key_id: str, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        sig = self._sign((ts + method.upper() + path.split("?")[0]).encode())
        return {"KALSHI-ACCESS-KEY": key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}


def load_keys() -> dict:
    try:
        keys = json.loads(KEYS_FILE.read_text())
    except (OSError, ValueError):
        keys = {}
    if os.environ.get("KALSHI_KEY_ID") and os.environ.get("KALSHI_KEY_FILE"):
        keys.setdefault("real", {"key_id": os.environ["KALSHI_KEY_ID"], "key_file": os.environ["KALSHI_KEY_FILE"]})
    return keys


# ------------------------------------------------------------------- client
class Client:
    def __init__(self, env: str = "real"):
        self.env = env
        self.bases = BASES[env]
        self.base = self.bases[0]
        k = load_keys().get(env) or {}
        self.key_id = str(k.get("key_id", "")).strip()
        self.signer, self.key_error = None, None
        if self.key_id and k.get("key_file"):
            try:
                kp = Path(str(k["key_file"]).strip().strip('"')).expanduser()
                self.signer = Signer(kp.read_text(encoding="utf-8", errors="replace"))
            except Exception as e:                                   # noqa: BLE001
                self.key_error = f"could not read the private key: {e}"
        else:
            self.key_error = f"no {env} API key in {KEYS_FILE.name}"
        self._local = threading.local()
        self._shards: dict[str, int] = {}
        self._bal_lock = threading.Lock()
        self._bal_t = 0.0
        self.post_only = True

    @property
    def has_keys(self) -> bool:
        return self.signer is not None

    # ---- transport
    def _send(self, method, url, hdr, data, timeout):
        if urllib.request.getproxies().get("https"):
            req = urllib.request.Request(url, data=data, headers=hdr, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()
        u = urllib.parse.urlsplit(url)
        pool = self._local.__dict__.setdefault("pool", {})
        path = u.path + ("?" + u.query if u.query else "")
        for attempt in (0, 1):
            c = pool.get(u.netloc) or pool.setdefault(u.netloc, http.client.HTTPSConnection(u.netloc, timeout=timeout))
            try:
                c.timeout = timeout
                if c.sock is not None:
                    c.sock.settimeout(timeout)
                c.request(method, path, body=data, headers=hdr)
                r = c.getresponse()
                body = r.read()
                if r.will_close:
                    c.close()
                    pool.pop(u.netloc, None)
                return r.status, body
            except (http.client.HTTPException, OSError):
                c.close()
                pool.pop(u.netloc, None)
                if attempt:
                    raise

    def request(self, method, path, params=None, body=None, auth=True, timeout=8.0, base=None):
        for wait in (1.0, 2.0, 4.0, None):                # 429 = not processed, safe to retry
            try:
                return self._request(method, path, params, body, auth, timeout, base)
            except KalshiError as e:
                if e.status != 429 or wait is None:
                    raise
                time.sleep(wait)

    def _request(self, method, path, params, body, auth, timeout, base):
        if auth and not self.signer:
            raise KalshiError(self.key_error or "no API key")
        q = "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}) if params else ""
        bases = [base] if base else [self.base] + [b for b in self.bases if b != self.base]
        last = None
        for b in bases:
            hdr = {"Accept": "application/json", "User-Agent": "kalshi-maker-lab/1.0"}
            if auth:
                hdr.update(self.signer.headers(self.key_id, method, urllib.parse.urlparse(b).path + path))
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                hdr["Content-Type"] = "application/json"
            try:
                status, raw = self._send(method, b + path + q, hdr, data, timeout)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                continue
            if not base:
                self.base = b
            if status < 400:
                return json.loads(raw) if raw else {}
            try:
                d = json.loads(raw or b"{}")
                err = d.get("error") or d
                msg = (err.get("message") or err.get("code")) if isinstance(err, dict) else str(err)
            except ValueError:
                msg = raw[:120].decode(errors="replace")
            raise KalshiError(f"HTTP {status}: {msg}", status)
        raise KalshiError(f"cannot reach Kalshi ({last})")

    # ---- public market data (live exchange)
    def get_public(self, path, params=None, timeout=4.0):
        return self.request("GET", path, params, auth=False, timeout=timeout, base=PUBLIC)

    def open_market(self, series: str) -> dict | None:
        now = time.time()
        for m in self.get_public("/markets", {"series_ticker": series, "status": "open", "limit": 10}).get("markets") or []:
            if iso_ts(m["open_time"]) <= now < iso_ts(m["close_time"]):
                return m
        return None

    def find_series(self, words: str) -> str | None:
        """A 15-minute series whose title contains `words` (e.g. 'natural gas'), from Kalshi's series list."""
        if not hasattr(self, "_series"):
            self._series = []
            for cat in ("Commodities", "Crypto", "Financials"):
                try:
                    self._series += self.get_public("/series", {"category": cat}, timeout=8).get("series") or []
                except KalshiError:
                    pass
        w = words.lower()
        for x in self._series:
            title, tk = str(x.get("title", "")).lower(), str(x.get("ticker", ""))
            if w in title and ("15" in title or tk.upper().endswith("15M")):
                return tk
        return None

    def market(self, ticker: str) -> dict | None:
        try:
            return self.get_public(f"/markets/{ticker}").get("market")
        except KalshiError as e:
            if e.status == 404:
                return None
            raise

    def book(self, ticker: str) -> dict | None:
        """Best YES bid/ask in cents and sizes, live from the order book. Kalshi lists bids only,
        ascending: YES ask = 100 - best NO bid."""
        ob = self.get_public(f"/markets/{ticker}/orderbook", timeout=3.0)
        ob = ob.get("orderbook_fp") or ob.get("orderbook") or {}

        def best(dkey, ckey):
            lv = []
            for x in ob.get(dkey) or []:
                try:
                    if float(x[1]) > 0:
                        lv.append((float(x[0]) * 100, float(x[1])))
                except (TypeError, ValueError, IndexError):
                    pass
            if not lv:
                for x in ob.get(ckey) or []:
                    try:
                        if float(x[1]) > 0:
                            lv.append((float(x[0]), float(x[1])))
                    except (TypeError, ValueError, IndexError):
                        pass
            return max(lv) if lv else None
        y, n = best("yes_dollars", "yes"), best("no_dollars", "no")
        if not y or not n:
            return None
        return {"bid": y[0], "ask": 100 - n[0], "bid_size": y[1], "ask_size": n[1]}

    # ---- account
    def balance(self) -> dict:
        with self._bal_lock:                          # 1 balance call per second
            wait = self._bal_t + 1.05 - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._bal_t = time.monotonic()
        return self.request("GET", "/portfolio/balance")

    def shard_cash(self, bal=None) -> dict[int, float]:
        b = bal if bal is not None else self.balance()
        out = {}
        for x in b.get("balance_breakdown") or []:
            try:
                out[int(x["exchange_index"])] = float(x["balance"]) * 100
            except (KeyError, TypeError, ValueError):
                pass
        return out or {0: num(b, "balance", 0)}

    def market_shard(self, ticker: str) -> int:
        if ticker not in self._shards:
            m = self.market(ticker) or {}
            try:
                self._shards[ticker] = int(m.get("exchange_index"))
            except (TypeError, ValueError):
                self._shards[ticker] = 0
        return self._shards[ticker]

    def ensure_shard_cash(self, ticker: str, need_c: float, top_up_c: float) -> tuple[bool, str]:
        """Kalshi keeps cash per exchange shard and API orders aren't funded from the others."""
        dst = self.market_shard(ticker)
        cash = self.shard_cash()
        have = cash.get(dst, 0.0)
        if have >= need_c:
            return True, ""
        want, moved = max(top_up_c, need_c) - have, 0
        for src, amt in sorted(cash.items(), key=lambda kv: -kv[1]):
            if src == dst or amt <= 0 or moved >= want:
                continue
            step = int(min(amt, want - moved))
            self.request("POST", "/portfolio/intra_exchange_instance_transfer", body={
                "source": "event_contract", "destination": "event_contract",
                "source_exchange_shard": src, "destination_exchange_shard": dst,
                "amount": step * 100, "client_transfer_id": str(uuid.uuid4())})
            moved += step
        if moved <= 0:
            return False, f"no spare cash (shard {dst} has ${have / 100:.2f})"
        for _ in range(8):
            if self.shard_cash().get(dst, 0.0) >= need_c:
                return True, f"moved ${moved / 100:.2f} to shard {dst}"
        return False, "transfer not arrived yet"

    def position(self, ticker: str) -> int:
        r = self.request("GET", "/portfolio/positions", {"ticker": ticker, "limit": 10})
        for p in r.get("market_positions") or []:
            if p.get("ticker") == ticker:
                return int(round(num(p, "position", 0)))
        return 0

    # ---- orders (V2: everything is quoted on the YES book; buying NO at p = asking YES at 100-p)
    def place_resting_buy(self, ticker: str, side: str, count: int, price_c: float, expires_ts: float) -> dict:
        """price_c may have tenths of a cent (Kalshi quotes these markets in $0.001 steps)."""
        yes_px = round(price_c if side == "YES" else 100 - price_c, 2)
        body = {"ticker": ticker, "side": "bid" if side == "YES" else "ask", "count": f"{int(count)}.00",
                "price": f"{yes_px / 100:.4f}", "time_in_force": "good_till_canceled",
                "self_trade_prevention_type": "taker_at_cross", "expiration_time": int(expires_ts),
                "client_order_id": str(uuid.uuid4())}
        if self.post_only:
            body["post_only"] = True                      # maker only: Kalshi rejects it rather than taking an offer
        try:
            r = self.request("POST", "/portfolio/events/orders", body=body)
        except KalshiError as e:
            msg = str(e).lower()
            if self.post_only and e.status == 400 and "post" in msg and ("cross" in msg or "taker" in msg
                                                                        or "match" in msg or "would" in msg):
                return {"order_id": "", "fill_count": 0, "rejected": "price moved - would have taken an offer"}
            if self.post_only and e.status == 400 and "post" in msg:
                self.post_only = False                    # this endpoint doesn't take the flag: send without it
                body.pop("post_only", None)
                body["client_order_id"] = str(uuid.uuid4())
                r = self.request("POST", "/portfolio/events/orders", body=body)
            else:
                raise
        r = r.get("order", r) if isinstance(r, dict) else {}
        return {"order_id": r.get("order_id", ""), "fill_count": int(num(r, "fill_count", 0)),
                "taker_fees_c": num(r, "taker_fees", 0) or 0}

    def sell_ioc(self, ticker: str, side: str, count: int, min_price_c: float) -> dict:
        """Sell what we hold right away at the best bids down to min_price_c; the rest is cancelled.
        Reduce-only, so it can never open the opposite position."""
        yes_px = round(min_price_c if side == "YES" else 100 - min_price_c, 2)
        body = {"ticker": ticker, "side": "ask" if side == "YES" else "bid", "count": f"{int(count)}.00",
                "price": f"{yes_px / 100:.4f}", "time_in_force": "immediate_or_cancel", "reduce_only": True,
                "self_trade_prevention_type": "taker_at_cross", "client_order_id": str(uuid.uuid4())}
        r = self.request("POST", "/portfolio/events/orders", body=body)
        r = r.get("order", r) if isinstance(r, dict) else {}
        got = int(num(r, "fill_count", 0))
        afp = r.get("average_fill_price")                  # dollars, YES book
        avg = None
        if afp not in (None, ""):
            avg = float(afp) * 100 if side == "YES" else 100 - float(afp) * 100
        fee = r.get("average_fee_paid")
        return {"fill_count": got, "avg_price_c": avg,
                "fees_c": got * float(fee) * 100 if fee not in (None, "") else (num(r, "taker_fees", 0) or 0)}

    def get_order(self, order_id: str) -> dict:
        """Kalshi can take a moment to list a brand-new order: a 404 then means 'not visible yet'."""
        try:
            return self.request("GET", f"/portfolio/orders/{order_id}").get("order") or {}
        except KalshiError as e:
            if e.status == 404:
                return {"status": "not_visible_yet"}
            raise

    def cancel(self, order_id: str, ticker: str):
        # the ticker routes the cancel to the right exchange shard (without it Kalshi answers 404)
        return self.request("DELETE", f"/portfolio/events/orders/{order_id}", {"market_ticker": ticker})

    def cancel_all(self):
        return self.request("DELETE", "/portfolio/events/orders")


def key_info(pem: str) -> dict:
    """Type and a short fingerprint of a private key, so a person can tell keys apart without seeing them.
    Raises ValueError with a plain-English reason for files that aren't a usable private key."""
    _, der = _pem_body(pem)
    k = private_key(pem)
    kind = f"RSA {k.bits}-bit" if isinstance(k, _PureRSA) else "Ed25519"
    return {"type": kind, "fingerprint": hashlib.sha256(der).hexdigest()[:16]}
