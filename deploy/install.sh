#!/usr/bin/env bash
# Sets the program up as a service on a Linux server. Run from the project folder:  ./deploy/install.sh
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$(pwd)"
USER_NAME="$(id -un)"
PY="${PYTHON:-python3}"

"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || { echo "Python 3.10+ is required"; exit 1; }
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m unittest discover -s tests >/dev/null 2>&1 && echo "self-tests passed" || echo "WARNING: self-tests failed (run: .venv/bin/python -m unittest discover -s tests -v)"

if [ ! -f keys.json ]; then
  echo "No keys.json yet. Copy keys.example.json to keys.json and fill it in; until then the program uses public REST polling."
fi

UNIT=/tmp/kalshi-maker-lab.service
sed -e "s|__DIR__|$DIR|g" -e "s|__USER__|$USER_NAME|g" deploy/kalshi-maker-lab.service > "$UNIT"
echo
echo "To install and start the service:"
echo "  sudo cp $UNIT /etc/systemd/system/kalshi-maker-lab.service"
echo "  sudo systemctl daemon-reload && sudo systemctl enable --now kalshi-maker-lab"
echo "  journalctl -u kalshi-maker-lab -f        # watch it; the web address and token are printed at start"
echo
echo "To open the web page from your own computer (keeps the port closed to the internet):"
echo "  ssh -L 8787:127.0.0.1:8787 $USER_NAME@<server-address>     then browse to the address printed in the log"
