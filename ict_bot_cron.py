#!/usr/bin/env python3
"""
ICT 5m Signal Bot - GitHub Actions Cron-Variante.

Fuehrt EINEN Durchlauf aus: Kursdaten holen -> ICT-Setups suchen (Liquidity
Sweep + Break of Structure, dann Einstieg per Fair Value Gap / Order Block /
Optimal Trade Entry) -> bei neuem Setup eine Discord-Nachricht schicken ->
Zustand in signals.json speichern -> beenden.

Wird von .github/workflows/ict-bot.yml automatisch alle 5 Minuten gestartet.
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
LOOKBACK_CANDLES = 40

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
def fetch_klines(symbol: str):
    params = {"symbol": symbol, "interval": "5m", "limit": 150}
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


# ---------- ICT-Logik ----------
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


def find_fvg_in_range(candles, frm, to, direction):
    for i in range(max(frm + 1, 2), to + 1):
        a, c = candles[i - 2], candles[i]
        if direction == "LONG" and a["high"] < c["low"]:
            return {"low": a["high"], "high": c["low"]}
        if direction == "SHORT" and a["low"] > c["high"]:
            return {"low": c["high"], "high": a["low"]}
    return None


def find_last_opposite_candle(candles, frm, to, direction):
    for k in range(to, frm - 1, -1):
        c = candles[k]
        if direction == "LONG" and c["close"] < c["open"]:
            return c
        if direction == "SHORT" and c["close"] > c["open"]:
            return c
    return None


def find_impulse_extreme(candles, frm, to, want_high):
    vals = [c["high" if want_high else "low"] for c in candles[frm:to + 1]]
    return max(vals) if want_high else min(vals)


def find_bullish_event(candles, highs, lows, look_start):
    n = len(candles)
    for li in range(len(lows) - 1, 0, -1):
        swing_low = lows[li]
        if swing_low["idx"] < look_start:
            break
        for i in range(swing_low["idx"] + 1, n):
            c = candles[i]
            if c["low"] < swing_low["price"] and c["close"] > swing_low["price"]:
                ref_candidates = [h for h in highs if swing_low["idx"] - 5 < h["idx"] <= i]
                ref_high = ref_candidates[-1] if ref_candidates else (highs[-1] if highs else None)
                if not ref_high:
                    continue
                for j in range(i, n):
                    if candles[j]["close"] > ref_high["price"]:
                        return {"sweep_idx": i, "bos_idx": j, "sweep_candle": c}
    return None


def find_bearish_event(candles, highs, lows, look_start):
    n = len(candles)
    for hi in range(len(highs) - 1, 0, -1):
        swing_high = highs[hi]
        if swing_high["idx"] < look_start:
            break
        for i in range(swing_high["idx"] + 1, n):
            c = candles[i]
            if c["high"] > swing_high["price"] and c["close"] < swing_high["price"]:
                ref_candidates = [l for l in lows if swing_high["idx"] - 5 < l["idx"] <= i]
                ref_low = ref_candidates[-1] if ref_candidates else (lows[-1] if lows else None)
                if not ref_low:
                    continue
                for j in range(i, n):
                    if candles[j]["close"] < ref_low["price"]:
                        return {"sweep_idx": i, "bos_idx": j, "sweep_candle": c}
    return None


def make_setup(symbol, direction, setup_type, sl_basis, entry, opposing_prices, zone):
    sl = sl_basis * (1 - BUFFER_PCT) if direction == "LONG" else sl_basis * (1 + BUFFER_PCT)
    if direction == "LONG":
        targets = sorted([p for p in opposing_prices if p > entry])
        tp = targets[0] if targets else entry + 2 * (entry - sl)
        risk = entry - sl
    else:
        targets = sorted([p for p in opposing_prices if p < entry], reverse=True)
        tp = targets[0] if targets else entry - 2 * (sl - entry)
        risk = sl - entry
    if risk <= 0:
        return None
    rr = (tp - entry) / risk if direction == "LONG" else (entry - tp) / risk
    if rr <= 0:
        return None
    return {"symbol": symbol, "direction": direction, "type": setup_type,
            "entry": entry, "sl": sl, "tp": tp, "rr": rr, "zone": zone}


def detect_setups(symbol, candles):
    results = []
    highs, lows = find_swings(candles)
    if len(highs) < 3 or len(lows) < 3:
        return results
    n = len(candles)
    look_start = max(0, n - LOOKBACK_CANDLES)
    current = candles[-1]

    bull = find_bullish_event(candles, highs, lows, look_start)
    if bull:
        i, j, c = bull["sweep_idx"], bull["bos_idx"], bull["sweep_candle"]
        sweep_low = c["low"]
        if current["close"] > sweep_low:
            opposing = [h["price"] for h in highs]

            fvg = find_fvg_in_range(candles, i, j, "LONG")
            if fvg and fvg["low"] <= current["close"] <= fvg["high"]:
                s = make_setup(symbol, "LONG", "FVG", sweep_low, current["close"], opposing,
                                {"low": fvg["low"], "high": fvg["high"]})
                if s:
                    s.update(id=f"{symbol}-LONG-FVG-{c['openTime']}", entryTime=current["openTime"])
                    results.append(s)

            ob = find_last_opposite_candle(candles, i, j, "LONG")
            if ob and ob["low"] <= current["close"] <= ob["high"]:
                s = make_setup(symbol, "LONG", "OB", ob["low"], current["close"], opposing,
                                {"low": ob["low"], "high": ob["high"]})
                if s:
                    s.update(id=f"{symbol}-LONG-OB-{c['openTime']}", entryTime=current["openTime"])
                    results.append(s)

            impulse_high = find_impulse_extreme(candles, i, j, True)
            rng = impulse_high - sweep_low
            ote_high = impulse_high - 0.618 * rng
            ote_low = impulse_high - 0.79 * rng
            if rng > 0 and ote_low <= current["close"] <= ote_high:
                s = make_setup(symbol, "LONG", "OTE", sweep_low, current["close"], opposing,
                                {"low": ote_low, "high": ote_high})
                if s:
                    s.update(id=f"{symbol}-LONG-OTE-{c['openTime']}", entryTime=current["openTime"])
                    results.append(s)

    bear = find_bearish_event(candles, highs, lows, look_start)
    if bear:
        i, j, c = bear["sweep_idx"], bear["bos_idx"], bear["sweep_candle"]
        sweep_high = c["high"]
        if current["close"] < sweep_high:
            opposing = [l["price"] for l in lows]

            fvg = find_fvg_in_range(candles, i, j, "SHORT")
            if fvg and fvg["low"] <= current["close"] <= fvg["high"]:
                s = make_setup(symbol, "SHORT", "FVG", sweep_high, current["close"], opposing,
                                {"low": fvg["low"], "high": fvg["high"]})
                if s:
                    s.update(id=f"{symbol}-SHORT-FVG-{c['openTime']}", entryTime=current["openTime"])
                    results.append(s)

            ob = find_last_opposite_candle(candles, i, j, "SHORT")
            if ob and ob["low"] <= current["close"] <= ob["high"]:
                s = make_setup(symbol, "SHORT", "OB", ob["high"], current["close"], opposing,
                                {"low": ob["low"], "high": ob["high"]})
                if s:
                    s.update(id=f"{symbol}-SHORT-OB-{c['openTime']}", entryTime=current["openTime"])
                    results.append(s)

            impulse_low = find_impulse_extreme(candles, i, j, False)
            rng = sweep_high - impulse_low
            ote_low = impulse_low + 0.618 * rng
            ote_high = impulse_low + 0.79 * rng
            if rng > 0 and ote_low <= current["close"] <= ote_high:
                s = make_setup(symbol, "SHORT", "OTE", sweep_high, current["close"], opposing,
                                {"low": ote_low, "high": ote_high})
                if s:
                    s.update(id=f"{symbol}-SHORT-OTE-{c['openTime']}", entryTime=current["openTime"])
                    results.append(s)

    return results


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
    """Zeitstempel, wann ein Setup vom Bot gefunden wurde (nicht die Kerzenzeit)."""
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
            candles = fetch_klines(symbol)
            if len(candles) < 40:
                continue

            last_closed_time = candles[-1]["closeTime"]
            if state["last_closed"].get(symbol) != last_closed_time:
                state["last_closed"][symbol] = last_closed_time
                for setup in detect_setups(symbol, candles):
                    if any(s["id"] == setup["id"] for s in state["signals"]):
                        continue
                    setup["status"] = "OPEN"
                    setup["foundAt"] = now_str()
                    state["signals"].insert(0, setup)
                    msg = (
                        f"{setup['direction']} {setup['symbol']} · {setup['type']}\n"
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
                    outcome = evaluate_outcome(sig, candles)
                    if outcome != "OPEN":
                        sig["status"] = outcome
                        send_discord(f"{sig['direction']} {sig['symbol']} ({sig['type']}) -> {outcome}")

        except Exception as e:
            log.error("Fehler bei %s: %s", symbol, e)

    save_state(state)


if __name__ == "__main__":
    if os.environ.get("TEST_MESSAGE", "nein").strip().lower() in ("ja", "yes", "true", "1"):
        send_discord(f"✅ Testnachricht vom ICT-Bot — Discord-Verbindung funktioniert! ({now_str()})")
    else:
        run_once()
