#!/usr/bin/env python3
"""
ICT Multi-Timeframe Signal Bot - GitHub Actions Cron-Variante.

Signal nur wenn ALLE drei Bedingungen zusammenkommen:
  1. Bias auf dem 4H-Chart (Daily-Bias-Ersatz) ist bullisch oder baerisch
  2. Liquidity Sweep auf dem 1H-Chart in Richtung des Bias
  3. Break of Structure auf dem 5m-Chart NACH dem 1H-Sweep, in Bias-Richtung

Fuehrt EINEN Durchlauf aus und beendet sich. Wird von
.github/workflows/ict-bot.yml automatisch alle 5 Minuten gestartet.
Kein eigener Server noetig.
"""

import os
import json
import logging
from datetime import datetime, timezone

import requests

# ---------- Konfiguration ----------
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")  # z.B. https://DEINUSER.github.io/DEINREPO
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
BUFFER_PCT = 0.0015
STATE_FILE = "signals.json"
SWING_N = 2

BIAS_INTERVAL = "4h"     # Alternativ: "1d" fuer reinen Daily-Bias
SWEEP_INTERVAL = "1h"
LTF_INTERVAL = "5m"      # Alternativ: "15m"
SWEEP_LOOKBACK = 30      # wie viele 1H-Kerzen zurueck nach einem Sweep gesucht wird

# oeffentliche Marktdaten-Adresse zuerst (nicht geo-gesperrt), dann Fallbacks
KLINE_HOSTS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("ict_bot")


# ---------- Discord ----------
def send_discord(text: str):
    if not DISCORD_WEBHOOK_URL:
        log.warning("DISCORD_WEBHOOK_URL nicht gesetzt — nur geloggt:\n%s", text)
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": text}, timeout=10)
    except Exception as e:
        log.error("Discord-Versand fehlgeschlagen: %s", e)


# ---------- Marktdaten ----------
def fetch_klines(symbol: str, interval: str, limit: int = 150):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    last_error = None
    for host in KLINE_HOSTS:
        try:
            r = requests.get(f"{host}/api/v3/klines", params=params, timeout=10)
            r.raise_for_status()
            raw = r.json()
            candles = [{
                "openTime": k[0], "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]), "closeTime": k[6],
            } for k in raw]
            candles.pop()  # letzte, noch offene Kerze verwerfen
            return candles
        except Exception as e:
            last_error = e
            continue
    raise last_error


# ---------- Struktur / Swings ----------
def find_swings(candles, n=SWING_N):
    highs, lows = [], []
    for i in range(n, len(candles) - n):
        is_high = all(candles[j]["high"] <= candles[i]["high"] for j in range(i - n, i + n + 1) if j != i)
        is_low = all(candles[j]["low"] >= candles[i]["low"] for j in range(i - n, i + n + 1) if j != i)
        if is_high:
            highs.append({"idx": i, "price": candles[i]["high"]})
        if is_low:
            lows.append({"idx": i, "price": candles[i]["low"]})
    return highs, lows


def bias_from_swings(highs, lows):
    if len(highs) < 2 or len(lows) < 2:
        return "neutral"
    h1, h2 = highs[-2], highs[-1]
    l1, l2 = lows[-2], lows[-1]
    if h2["price"] > h1["price"] and l2["price"] > l1["price"]:
        return "bullish"
    if h2["price"] < h1["price"] and l2["price"] < l1["price"]:
        return "bearish"
    return "neutral"


# ---------- Schritt 1: HTF-Bias ----------
def get_bias(symbol):
    candles = fetch_klines(symbol, BIAS_INTERVAL, limit=120)
    if len(candles) < 20:
        return "neutral"
    highs, lows = find_swings(candles)
    return bias_from_swings(highs, lows)


# ---------- Schritt 2: 1H Liquidity Sweep ----------
def find_1h_sweep(symbol, bias):
    candles = fetch_klines(symbol, SWEEP_INTERVAL, limit=150)
    if len(candles) < 20:
        return None
    highs, lows = find_swings(candles)
    n = len(candles)
    look_start = max(0, n - SWEEP_LOOKBACK)

    if bias == "bullish":
        for li in range(len(lows) - 1, 0, -1):
            swing_low = lows[li]
            if swing_low["idx"] < look_start:
                break
            for i in range(swing_low["idx"] + 1, n):
                c = candles[i]
                if c["low"] < swing_low["price"] and c["close"] > swing_low["price"]:
                    opposing = [h["price"] for h in highs if h["idx"] > swing_low["idx"]]
                    return {"price": c["low"], "time": c["closeTime"], "opposing": opposing}
        return None
    else:
        for hi in range(len(highs) - 1, 0, -1):
            swing_high = highs[hi]
            if swing_high["idx"] < look_start:
                break
            for i in range(swing_high["idx"] + 1, n):
                c = candles[i]
                if c["high"] > swing_high["price"] and c["close"] < swing_high["price"]:
                    opposing = [l["price"] for l in lows if l["idx"] > swing_high["idx"]]
                    return {"price": c["high"], "time": c["closeTime"], "opposing": opposing}
        return None


# ---------- Schritt 3: LTF Break of Structure ----------
def find_ltf_bos(ltf_candles_after_sweep, bias):
    """Gibt nur ein Ergebnis zurueck, wenn der Break GENAU auf der letzten
    (aktuellsten) Kerze passiert ist -- verhindert doppelte Signale bei
    wiederholten Laeufen fuer denselben, bereits vergangenen Break."""
    n = len(ltf_candles_after_sweep)
    if n < 6:
        return None
    highs, lows = find_swings(ltf_candles_after_sweep)

    if bias == "bullish":
        if not highs:
            return None
        ref = highs[0]
        for j in range(ref["idx"] + 1, n):
            if ltf_candles_after_sweep[j]["close"] > ref["price"]:
                if j == n - 1:
                    return {"entry": ltf_candles_after_sweep[j]["close"]}
                return None
        return None
    else:
        if not lows:
            return None
        ref = lows[0]
        for j in range(ref["idx"] + 1, n):
            if ltf_candles_after_sweep[j]["close"] < ref["price"]:
                if j == n - 1:
                    return {"entry": ltf_candles_after_sweep[j]["close"]}
                return None
        return None


# ---------- Zusammenfuehren ----------
def build_setup(symbol, direction, entry, sweep_price, opposing):
    if direction == "LONG":
        sl = sweep_price * (1 - BUFFER_PCT)
        targets = sorted([p for p in opposing if p > entry])
        tp = targets[0] if targets else entry + 2 * (entry - sl)
        risk = entry - sl
        rr = (tp - entry) / risk if risk > 0 else 0
    else:
        sl = sweep_price * (1 + BUFFER_PCT)
        targets = sorted([p for p in opposing if p < entry], reverse=True)
        tp = targets[0] if targets else entry - 2 * (sl - entry)
        risk = sl - entry
        rr = (entry - tp) / risk if risk > 0 else 0
    if rr <= 0:
        return None
    return {"symbol": symbol, "direction": direction, "type": "MTF",
            "entry": entry, "sl": sl, "tp": tp, "rr": rr}


def detect_mtf_setup(symbol, ltf_candles):
    bias = get_bias(symbol)
    if bias == "neutral":
        return None

    sweep = find_1h_sweep(symbol, bias)
    if not sweep:
        return None

    after = [c for c in ltf_candles if c["openTime"] >= sweep["time"]]
    bos = find_ltf_bos(after, bias)
    if not bos:
        return None

    direction = "LONG" if bias == "bullish" else "SHORT"
    setup = build_setup(symbol, direction, bos["entry"], sweep["price"], sweep["opposing"])
    if not setup:
        return None

    current = ltf_candles[-1]
    setup["id"] = f"{symbol}-{direction}-MTF-{sweep['time']}-{current['openTime']}"
    setup["entryTime"] = current["openTime"]
    setup["zone"] = None
    setup["bias"] = bias
    return setup


def evaluate_outcome(sig, candles):
    relevant = [c for c in candles if c["openTime"] > sig["entryTime"]]
    for c in relevant:
        if sig["direction"] == "LONG":
            if c["low"] <= sig["sl"]:
                return "LOSS"
            if c["high"] >= sig["tp"]:
                return "WIN"
        else:
            if c["high"] >= sig["sl"]:
                return "LOSS"
            if c["low"] <= sig["tp"]:
                return "WIN"
    return "OPEN"


def fmt(n):
    return f"{n:,.2f}" if n >= 1000 else f"{n:,.4f}"


def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"signals": [], "last_closed": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def run_once():
    state = load_state()
    for symbol in SYMBOLS:
        try:
            ltf_candles = fetch_klines(symbol, LTF_INTERVAL, limit=200)
            if len(ltf_candles) < 50:
                continue

            last_closed_time = ltf_candles[-1]["closeTime"]
            if state["last_closed"].get(symbol) != last_closed_time:
                state["last_closed"][symbol] = last_closed_time
                setup = detect_mtf_setup(symbol, ltf_candles)
                if setup and not any(s["id"] == setup["id"] for s in state["signals"]):
                    setup["status"] = "OPEN"
                    setup["foundAt"] = now_str()
                    state["signals"].insert(0, setup)
                    bias_label = "bullisch" if setup["bias"] == "bullish" else "bärisch"
                    msg = (
                        f"{setup['direction']} {setup['symbol']} · MTF-Setup\n"
                        f"4H-Bias: {bias_label} · 1H-Sweep + {LTF_INTERVAL}-BOS bestätigt\n"
                        f"Entry: {fmt(setup['entry'])}\n"
                        f"SL: {fmt(setup['sl'])}\n"
                        f"TP: {fmt(setup['tp'])}\n"
                        f"RR: {setup['rr']:.2f}\n"
                        f"Gefunden: {setup['foundAt']}"
                    )
                    if PUBLIC_URL:
                        msg += f"\n\nChart ansehen: {PUBLIC_URL}/?symbol={setup['symbol']}"
                    log.info("Neues Signal: %s", msg.replace("\n", " | "))
                    send_discord(msg)

            for sig in state["signals"]:
                if sig["symbol"] == symbol and sig["status"] == "OPEN":
                    outcome = evaluate_outcome(sig, ltf_candles)
                    if outcome != "OPEN":
                        sig["status"] = outcome
                        send_discord(f"{sig['direction']} {sig['symbol']} (MTF) -> {outcome}")

        except Exception as e:
            log.error("Fehler bei %s: %s", symbol, e)

    save_state(state)


if __name__ == "__main__":
    if os.environ.get("TEST_MESSAGE", "nein").strip().lower() in ("ja", "yes", "true", "1"):
        send_discord(f"✅ Testnachricht vom ICT-Bot — Discord-Verbindung funktioniert! ({now_str()})")
    else:
        run_once()
