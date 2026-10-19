"""Subscription storage + USDT (TRC20) on-chain payment watcher.

Anti-fraud model: every user who starts a subscription gets a UNIQUE amount
(price + unique cents, e.g. 20.37). The watcher matches the exact incoming
amount to the pending user. No TXIDs, no screenshots, nothing to fake.
Used transaction ids are recorded so a payment can never be claimed twice.
"""
import json
import os
import random
import threading
import time

import requests

USDT_CONTRACT = "TR7NHqjeFYxGTCi8q8Zy4KoLJH7fJDwWahN"  # official USDT on TRON
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "subs.json")
PENDING_TTL = 24 * 3600          # a reserved amount stays valid 24 h

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

def get_or_create_pending(user_id: int, price: float) -> float:
    """Unique payable amount for this user, valid for PENDING_TTL."""
    with _lock:
        key = str(user_id)
        p = _state["pending"].get(key)
        if p and time.time() - p["created"] < PENDING_TTL:
            return p["amount"]
        taken = {x["amount"] for x in _state["pending"].values()}
        while True:
            amount = round(price + random.randint(11, 89) / 100, 2)
            if amount not in taken:
                break
        _state["pending"][key] = {"amount": amount, "created": time.time()}
        _save()
        return amount


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

def check_tron_payments(wallet: str) -> list[dict]:
    """Scan incoming USDT-TRC20 transfers; return newly matched payments."""
    if not wallet or wallet.startswith("PUT_"):
        return []
    try:
        r = requests.get(
            f"https://api.trongrid.io/v1/accounts/{wallet}/transactions/trc20",
            params={"only_to": "true", "limit": 50}, timeout=30)
        if r.status_code != 200:
            return []
        txs = r.json().get("data", [])
    except Exception:
        return []

    found = []
    with _lock:
        used = set(_state["used_txids"])
        pendings = dict(_state["pending"])

    for tx in txs:
        txid = tx.get("transaction_id")
        if not txid or txid in used:
            continue
        if (tx.get("token_info") or {}).get("address") != USDT_CONTRACT:
            continue
        if tx.get("to") != wallet:
            continue
        try:
            amount = round(int(tx["value"]) / 1_000_000, 2)  # USDT = 6 decimals
        except Exception:
            continue

        match = None
        for uid, p in pendings.items():
            if p["amount"] == amount and time.time() - p["created"] < PENDING_TTL:
                match = uid
                break

        with _lock:  # burn every seen txid — matched or not — to stop replays
            _state["used_txids"].append(txid)
            _state["used_txids"] = _state["used_txids"][-5000:]
            _save()

        if match:
            found.append({"user_id": int(match), "txid": txid, "amount": amount})
    return found
