#!/usr/bin/env python3
"""Kalshi Maker Lab - start the program.

  python run.py                 paper mode: records data, scores every rule, places no orders
  python run.py --allow-live    also unlocks the live-trading switch on the web page (still off until you turn it on)
  python run.py --host 0.0.0.0  listen on all addresses (prefer an SSH tunnel: see README)
"""
from __future__ import annotations

import argparse
import asyncio
import sys

if sys.version_info < (3, 10):
    sys.exit("Python 3.10 or newer is required")

try:
    from aiohttp import web
except ImportError:
    sys.exit("Missing dependency. Run:  pip install -r requirements.txt")

from kml import config as C
from kml.app import App
from kml.web import make_app


async def main(args):
    cfg = C.load()
    if args.host:
        cfg["web"]["host"] = args.host
    if args.port:
        cfg["web"]["port"] = args.port
    if args.source:
        cfg["source"] = args.source
    app = App(cfg, allow_live=args.allow_live)
    runner = web.AppRunner(make_app(app), access_log=None)
    await runner.setup()
    host, port = cfg["web"]["host"], int(cfg["web"]["port"])
    await web.TCPSite(runner, host, port).start()
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"\nKalshi Maker Lab is running.\n  Web page:  http://{shown}:{port}/?token={cfg['web']['token']}")
    print(f"  Data:      {app.source} feed, recording {'on' if app.rec.on else 'off'}")
    print(f"  Trading:   paper only" + ("  (live switch unlocked by --allow-live; it is still OFF until you turn it on)"
                                         if args.allow_live else "  (live trading locked: no --allow-live)"))
    if not app.client.has_keys:
        print(f"  Keys:      none loaded ({app.client.key_error}). Using public REST polling.")
    print("  Stop with Ctrl+C.\n")
    try:
        await app.run()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Kalshi Maker Lab")
    ap.add_argument("--allow-live", action="store_true", help="unlock the live-trading switch (it still starts off)")
    ap.add_argument("--host", help="address for the web page (default 127.0.0.1)")
    ap.add_argument("--port", type=int, help="port for the web page (default 8787)")
    ap.add_argument("--source", choices=["ws", "rest"], help="market data source (default ws)")
    try:
        asyncio.run(main(ap.parse_args()))
    except KeyboardInterrupt:
        print("\nstopped")
