#!/usr/bin/env python3
"""Kalshi Maker Lab - start the program.

  python run.py                 paper mode: records data, scores every rule, places no orders
  python run.py --setup         set the web login and whether the page is reachable on the local network
  python run.py --set-password  change the web password
  python run.py --allow-live    also unlocks the live-trading switch on the web page (still off until you turn it on)
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import secrets
import socket
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


def lan_ip() -> str | None:
    """This machine's address on the local network (no packet is actually sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
            return None if ip.startswith("127.") else ip
    except OSError:
        return None


def ask_password() -> str:
    while True:
        a = getpass.getpass("  Password (8 or more characters): ")
        if len(a) < 8:
            print("  Too short.")
            continue
        if a == getpass.getpass("  Again: "):
            return a
        print("  They didn't match.")


def setup(password_only: bool = False) -> None:
    cfg = C.load()
    w = cfg["web"]
    if password_only:
        C.set_login(password=ask_password())
        print("Password changed. Restart the program for it to take effect.")
        return
    print("Web page setup")
    user = input(f"  Username [{w.get('username', 'admin')}]: ").strip() or w.get("username", "admin")
    pw = ask_password()
    lan = input("  Make the page reachable from other devices on your local network? [Y/n]: ").strip().lower()
    host = "127.0.0.1" if lan.startswith("n") else "0.0.0.0"
    C.set_login(username=user, password=pw, host=host)
    ip = lan_ip()
    where = f"http://{ip or '<this-machine>'}:{w['port']}" if host == "0.0.0.0" else f"http://localhost:{w['port']}"
    print(f"Saved. After the program starts, sign in at {where}")
    if host == "0.0.0.0":
        print("The page is plain HTTP: fine on a home network, but do not forward this port on your router.")
        print(f"If a firewall is on, allow the port from your network, for example:\n"
              f"  sudo ufw allow from 192.168.0.0/16 to any port {w['port']} proto tcp")


async def main(args):
    cfg = C.load()
    w = cfg["web"]
    if args.host:
        w["host"] = args.host
    if args.lan:
        w["host"] = "0.0.0.0"
    if args.port:
        w["port"] = args.port
    if args.source:
        cfg["source"] = args.source
    first_pw = None
    if not w.get("password_hash"):                      # never run --setup: make a login so the page is not open
        first_pw = secrets.token_urlsafe(9)
        C.set_login(username=w.get("username", "admin"), password=first_pw)
        w["password_hash"] = C.load()["web"]["password_hash"]
    app = App(cfg, allow_live=args.allow_live)
    runner = web.AppRunner(make_app(app), access_log=None)
    await runner.setup()
    host, port = w["host"], int(w["port"])
    await web.TCPSite(runner, host, port).start()
    print("\nKalshi Maker Lab is running.")
    print(f"  Web page:  http://localhost:{port}" + ("" if host != "127.0.0.1" else "   (this machine only)"))
    if host == "0.0.0.0":
        print(f"             http://{lan_ip() or '<this-machine-address>'}:{port}   (local network)")
    if first_pw:
        print(f"  Sign in:   {w.get('username', 'admin')} / {first_pw}")
        print("             This password is shown once. Change it with:  python run.py --set-password")
    else:
        print(f"  Sign in:   as {w.get('username', 'admin')}   (python run.py --set-password to change the password)")
    print(f"  Data:      {app.source} feed, recording {'on' if app.rec.on else 'off'}")
    print("  Trading:   paper only" + ("  (live switch unlocked by --allow-live; it is still OFF until you turn it on)"
                                        if args.allow_live else "  (live trading locked: no --allow-live)"))
    if not app.client.has_keys:
        print(f"  Keys:      none loaded ({app.client.key_error}). Using public REST polling.")
    print("  Stop with Ctrl+C.\n", flush=True)
    try:
        await app.run()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Kalshi Maker Lab")
    ap.add_argument("--setup", action="store_true", help="set the web login and local-network access, then exit")
    ap.add_argument("--set-password", action="store_true", help="change the web password, then exit")
    ap.add_argument("--allow-live", action="store_true", help="unlock the live-trading switch (it still starts off)")
    ap.add_argument("--lan", action="store_true", help="listen on the local network for this run")
    ap.add_argument("--host", help="address for the web page (default from config.json)")
    ap.add_argument("--port", type=int, help="port for the web page (default 8787)")
    ap.add_argument("--source", choices=["ws", "rest"], help="market data source (default ws)")
    a = ap.parse_args()
    try:
        if a.setup or a.set_password:
            setup(password_only=a.set_password and not a.setup)
        else:
            asyncio.run(main(a))
    except KeyboardInterrupt:
        print("\nstopped")
