"""Kalshi API key management for the web page.

Keys are stored the same way the program reads them: keys.json holds the key id and the path to the private key
file. Uploaded or pasted keys are written to secrets/<env>_private_key.pem (folder 0700, file 0600). The private
key is never sent back to the browser: the page only sees its type and a short fingerprint.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import kalshi_rest as KR

ENVS = {"real": "Production (real money)", "sandbox": "Demo (sandbox)"}
KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{5,99}$")
MAX_PEM = 20_000


def secrets_dir() -> Path:
    return KR.KEYS_FILE.parent / "secrets"


def _read() -> dict:
    try:
        return json.loads(KR.KEYS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _private_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _write(keys: dict) -> None:
    _private_write(KR.KEYS_FILE, json.dumps(keys, indent=1))


def mask(key_id: str) -> str:
    return key_id[:8] + "..." + key_id[-4:] if len(key_id) > 14 else key_id[:4] + "..."


def check_pem(pem: str) -> dict:
    """Validate a private key and return its type and fingerprint. Raises ValueError with a reason."""
    if len(pem) > MAX_PEM:
        raise ValueError("that file is too large to be a private key")
    pem = pem.replace("\r\n", "\n").strip() + "\n"
    try:
        info = KR.key_info(pem)
    except ValueError:
        raise
    except Exception:                                                  # noqa: BLE001
        raise ValueError("couldn't read that as a private key. Kalshi's key file is a text file that starts with "
                         "'-----BEGIN RSA PRIVATE KEY-----' or '-----BEGIN PRIVATE KEY-----'")
    try:
        KR.Signer(pem).headers("check", "GET", "/trade-api/v2/portfolio/balance")
    except Exception as e:                                             # noqa: BLE001
        raise ValueError(f"the key could not sign a request: {e}")
    return info


def describe() -> dict:
    keys, out = _read(), {}
    for env, label in ENVS.items():
        k = keys.get(env) or {}
        d = {"label": label, "present": bool(k.get("key_id") and k.get("key_file"))}
        if d["present"]:
            p = Path(str(k["key_file"]).strip().strip('"')).expanduser()
            d.update(key_id=mask(str(k["key_id"])), key_file=str(p), stored_here=_managed(p))
            try:
                d.update(check_pem(p.read_text(encoding="utf-8", errors="replace")))
            except OSError as e:
                d["error"] = f"can't read the key file: {e.strerror or e}"
            except ValueError as e:
                d["error"] = str(e)
        out[env] = d
    if os.environ.get("KALSHI_KEY_ID") and not (keys.get("real") or {}).get("key_id"):
        out["real"]["note"] = "a key is also set by the KALSHI_KEY_ID / KALSHI_KEY_FILE environment variables"
    return out


def _managed(p: Path) -> bool:
    try:
        return p.resolve().parent == secrets_dir().resolve()
    except OSError:
        return False


def save(env: str, key_id: str, pem: str | None = None, path: str | None = None) -> dict:
    """Store a key. Give the key file's text (upload or paste), a path to a file already on this machine, or
    neither to change only the key id. Returns the key's type and fingerprint."""
    if env not in ENVS:
        raise ValueError("unknown environment")
    key_id = (key_id or "").strip()
    if not KEY_ID_RE.match(key_id):
        raise ValueError("the key ID doesn't look right: copy it exactly as Kalshi shows it")
    keys = _read()
    if pem and pem.strip():
        info = check_pem(pem)
        sd = secrets_dir()
        sd.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(sd, 0o700)
        except OSError:
            pass
        dest = sd / f"{env}_private_key.pem"
        _private_write(dest, pem.replace("\r\n", "\n").strip() + "\n")
        key_file = str(dest.resolve())
    elif path and path.strip():
        p = Path(path.strip().strip('"')).expanduser()
        if not p.is_file():
            raise ValueError("there is no file at that path on this machine")
        try:
            info = check_pem(p.read_text(encoding="utf-8", errors="replace"))
        except OSError as e:
            raise ValueError(f"can't read that file: {e.strerror or e}")
        key_file = str(p.resolve())
    else:
        old = (keys.get(env) or {}).get("key_file")
        if not old:
            raise ValueError("add the private key file too: upload it, paste it, or give its path")
        p = Path(old).expanduser()
        try:
            info = check_pem(p.read_text(encoding="utf-8", errors="replace"))
        except OSError as e:
            raise ValueError(f"the stored key file can't be read ({e.strerror or e}): upload it again")
        key_file = str(p)
    keys[env] = {"key_id": key_id, "key_file": key_file}
    _write(keys)
    return info


def remove(env: str) -> None:
    keys = _read()
    k = keys.pop(env, None)
    _write(keys)
    if k and k.get("key_file"):
        p = Path(k["key_file"]).expanduser()
        if _managed(p):                     # only delete files this page stored; never a file the user pointed to
            try:
                p.unlink()
            except OSError:
                pass
