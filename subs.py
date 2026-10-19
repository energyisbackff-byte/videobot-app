"""Subscription storage + Bitcoin on-chain payment watcher.

Anti-fraud model: every user who starts a subscription gets a UNIQUE amount
in satoshis (price converted at the current BTC rate + a unique sat offset).
The watcher matches the exact incoming sat amount on-chain after the required
confirmations. No TXIDs, no screenshots, nothing to fake. Used txids are
recorded so a payment can never be claimed twice.
"""
import json
import os
import random
import threading
import time

import requests

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "subs.json")
PENDING_TTL = 2 * 3600           # a rate-locked quote stays valid 2 h
BLOCKSTREAM_API = "https://blockstream.info/api"
MEMPOOL_API = "https://mempool.space/api"

_lock = threading.Lock()
_state = {"subs": {}, "pending": {}, "used_txids": [], "usage": {}}


def _load():
    if os.path.exists(DB_PATH):
        try:
            with open(DB_PATH) as f:
                _state.update(json.load(f))
        except Exception:
            pass


def _save():
    tmp = DB_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_state, f)
    os.replace(tmp, DB_PATH)


_load()


# --------------------------- subscriptions ----------------------------------

def is_subscribed(user_id: int) -> bool:
    with _lock:
        s = _state["subs"].get(str(user_id))
        return bool(s and s["until"] > time.time())


def sub_info(user_id: int):
    with _lock:
        return _state["subs"].get(str(user_id))


def activate(user_id: int, days: int, txid: str = "", granted_by: str = "auto") -> float:
    """Activate/extend a subscription. Returns the new expiry timestamp."""
    with _lock:
        key = str(user_id)
        base = max(time.time(), _state["subs"].get(key, {}).get("until", 0))
        until = base + days * 86400
        _state["subs"][key] = {"until": until, "txid": txid, "granted_by": granted_by}
        _state["pending"].pop(key, None)
        _save()
        return until


def revoke(user_id: int) -> bool:
    with _lock:
        existed = str(user_id) in _state["subs"]
        _state["subs"].pop(str(user_id), None)
        _save()
        return existed


def list_subs() -> dict:
    with _lock:
        now = time.time()
        return {k: v for k, v in _state["subs"].items() if v["until"] > now}


# --------------------------- pending payments -------------------------------

def _btc_usd_rate() -> float | None:
    """Current BTC/USD rate from two independent public APIs."""
    try:
        r = requests.get(f"{MEMPOOL_API}/v1/prices", timeout=15)
        if r.status_code == 200:
            return float(r.json()["USD"])
    except Exception:
        pass
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd"}, timeout=15)
        if r.status_code == 200:
            return float(r.json()["bitcoin"]["usd"])
    except Exception:
        pass
    return None


def get_or_create_pending(user_id: int, price_usd: float) -> dict | None:
    """Unique satoshi amount for this user, rate-locked for PENDING_TTL."""
    with _lock:
        key = str(user_id)
        p = _state["pending"].get(key)
        if p and time.time() - p["created"] < PENDING_TTL:
            return dict(p)
    rate = _btc_usd_rate()
    if not rate:
        return None
    with _lock:
        taken = {x["sats"] for x in _state["pending"].values()}
        while True:
            sats = int(price_usd / rate * 100_000_000) + random.randint(120, 980)
            if sats not in taken:
                break
        p = {"sats": sats, "btc": f"{sats / 100_000_000:.8f}",
             "rate": rate, "created": time.time()}
        _state["pending"][key] = p
        _save()
        return dict(p)


# --------------------------- monthly usage ----------------------------------

def monthly_usage(user_id: int) -> int:
    with _lock:
        u = _state["usage"].get(str(user_id), {})
        return u.get("count", 0) if u.get("month") == time.strftime("%Y-%m") else 0


def increment_usage(user_id: int):
    with _lock:
        key, month = str(user_id), time.strftime("%Y-%m")
        u = _state["usage"].get(key, {})
        if u.get("month") != month:
            u = {"month": month, "count": 0}
        u["count"] += 1
        _state["usage"][key] = u
        _save()


# --------------------------- on-chain watcher -------------------------------

def check_btc_payments(address: str, confirmations: int = 1) -> list[dict]:
    """Scan incoming BTC transfers; return newly matched, confirmed payments."""
    if not address or address.startswith("PUT_"):
        return []
    try:
        r = requests.get(f"{BLOCKSTREAM_API}/address/{address}/txs", timeout=30)
        if r.status_code != 200:
            return []
        txs = r.json()
    except Exception:
        return []
    tip = None
    try:
        tip = int(requests.get(f"{BLOCKSTREAM_API}/blocks/tip/height",
                               timeout=15).text)
    except Exception:
        pass

    found = []
    with _lock:
        used = set(_state["used_txids"])
        pendings = dict(_state["pending"])

    for tx in txs[:50]:
        txid = tx.get("txid")
        if not txid or txid in used:
            continue
        st = tx.get("status") or {}
        if not st.get("confirmed"):
            continue
        if tip and confirmations > 1:
            confs = tip - int(st.get("block_height", 0)) + 1
            if confs < confirmations:
                continue

        amount = 0
        for out in tx.get("vout", []):
            if out.get("scriptpubkey_address") == address:
                amount += int(out.get("value", 0))
        if amount <= 0:
            continue

        match = None
        for uid, p in pendings.items():
            if p["sats"] == amount and time.time() - p["created"] < PENDING_TTL:
                match = uid
                break

        with _lock:  # burn every seen txid — matched or not — to stop replays
            _state["used_txids"].append(txid)
            _state["used_txids"] = _state["used_txids"][-5000:]
            _save()

        if match:
            found.append({"user_id": int(match), "txid": txid, "sats": amount,
                          "btc": f"{amount / 100_000_000:.8f}"})
    return found
